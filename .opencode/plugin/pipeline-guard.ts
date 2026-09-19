import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import { execFileSync } from "node:child_process"
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmdirSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { dirname, isAbsolute, join, relative, resolve } from "node:path"

const ROLES = ["lead", "architect", "developer", "reviewer"] as const
type Role = (typeof ROLES)[number]

const PIPELINE = ".pipeline"
const MUTATING_TOOLS = new Set(["edit", "write", "patch", "multiedit", "apply_patch"])
const GIT_READ_SUBCOMMANDS = new Set(["status", "diff", "log", "show"])
const GIT_LEAD_SUBCOMMANDS = new Set(["status", "diff", "log", "show", "add"])
const GIT_FLAGS_WITH_VALUE = new Set(["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"])
const COMMIT_BANNED = /(?:^|\s)(--amend|--all|-a|--patch|-p)(?=\s|$)/
const TASK_ID = /\bT-\d{8}-[A-Za-z0-9-]+\b/
const ROLE_MARKER = /ROLE:\s*(lead|architect|developer|reviewer)\b/i
const VERBOSE_SIGNAL = /(подробн|проще|попроще|по-простому|простыми словами|простым языком|для новичк|eli5|разжуй|verbose)/i
const BRIEF_SIGNAL = /(кратко|покороче|без подробност|обычн(?:ый|ом) режим|\/brief)/i
const ROUTE_VALUES = new Set(["full", "standard", "assisted"])
const ROUTE_SIGNALS: Array<{ route: string; re: RegExp }> = [
  { route: "full", re: /(\/full\b|полный маршрут|с дизайном)/i },
  { route: "standard", re: /(\/standard\b|в обход архитектора|без архитектора|без дизайна|сокращ[её]нн)/i },
  { route: "assisted", re: /(\/assisted\b|правку вн[её]с сам|правлю сам|я поправлю|без разработчика|сам исправлю|сам внесу)/i },
]
const WRITE_SIGNAL = /\bsed\s+-i|\brm\s|\bmv\s|\bcp\s|\btee\b|\bdd\s|\btruncate\b|>>?\s*(?!\/dev\/null)[^&\s]/
const VERDICT_FILE = /^T-\d{8}-[A-Za-z0-9-]+-a\d+\.(md|json)$/
const VERDICT_JSON = /^(T-\d{8}-[A-Za-z0-9-]+)-a(\d+)\.json$/
const REWORK_FILE = /^(T-\d{8}-[A-Za-z0-9-]+)-a(\d+)\.md$/

function isRole(value: string): value is Role {
  return (ROLES as readonly string[]).includes(value)
}

function relativeTo(dir: string, path: string): string {
  const absolute = isAbsolute(path) ? path : resolve(dir, path)
  return relative(dir, absolute).split("\\").join("/")
}

function under(rel: string, prefix: string): boolean {
  return rel === prefix || rel.startsWith(prefix + "/")
}

function gitSubcommand(segment: string): string {
  const match = segment.match(/\bgit\b(.*)$/)
  if (!match) return ""
  const tokens = match[1].trim().split(/\s+/)
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i]
    if (!token) continue
    if (GIT_FLAGS_WITH_VALUE.has(token)) {
      i++
      continue
    }
    if (token.startsWith("-")) continue
    return token
  }
  return ""
}

function gitSubcommands(command: string): string[] {
  return command
    .split(/\s*(?:&&|\|\||;|\|)\s*/)
    .map(gitSubcommand)
    .filter(Boolean)
}

const plugin: Plugin = async ({ directory }) => {
  const dir = resolve(directory)
  const roles = new Map<string, Role>()

  const deny = (message: string): never => {
    throw new Error(`pipeline-guard: ${message}`)
  }

  const stateFile = (taskId: string) => join(dir, PIPELINE, "state", `${taskId}.json`)
  const briefFile = (taskId: string) => join(dir, PIPELINE, "briefs", `${taskId}.md`)

  const readJson = <T,>(path: string, fallback: T): T => {
    try {
      return JSON.parse(readFileSync(path, "utf8")) as T
    } catch {
      return fallback
    }
  }

  const atomicWrite = (path: string, content: string) => {
    mkdirSync(dirname(path), { recursive: true })
    const tmp = `${path}.tmp-${process.pid}-${Math.random().toString(36).slice(2, 8)}`
    writeFileSync(tmp, content)
    renameSync(tmp, path)
  }

  const writeJson = (path: string, value: unknown) => {
    atomicWrite(path, JSON.stringify(value, null, 2))
  }

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

  const withLock = async (path: string, fn: () => void | Promise<void>) => {
    const lock = `${path}.lock`
    let acquired = false
    for (let attempt = 0; attempt < 40; attempt++) {
      try {
        mkdirSync(lock)
        acquired = true
        break
      } catch {
        await sleep(50)
      }
    }
    try {
      await fn()
    } finally {
      if (acquired) {
        try {
          rmdirSync(lock)
        } catch {}
      }
    }
  }

  const mutateState = async (path: string, mutate: (state: any) => void) => {
    await withLock(path, () => {
      const state = readJson<any>(path, null)
      if (!state) return
      mutate(state)
      state.updated_at = new Date().toISOString()
      writeJson(path, state)
    })
  }

  const sealAttempt = (kind: "verdicts" | "rework", taskId: string, attempt: number) => {
    try {
      const artifactDir = join(dir, PIPELINE, kind)
      if (!existsSync(artifactDir)) return
      const prefix = `${taskId}-a${attempt}.`
      const sealPath = join(artifactDir, `${taskId}-a${attempt}.seal`)
      if (existsSync(sealPath)) return
      const files: Record<string, string> = {}
      for (const name of readdirSync(artifactDir)) {
        if (!name.startsWith(prefix) || name.endsWith(".seal")) continue
        const path = join(artifactDir, name)
        if (!statSync(path).isFile()) continue
        files[name] = "sha256:" + createHash("sha256").update(readFileSync(path)).digest("hex")
      }
      if (Object.keys(files).length === 0) return
      writeJson(sealPath, { task_id: taskId, attempt, at: new Date().toISOString(), files })
    } catch {}
  }

  const verifySeal = (kind: "verdicts" | "rework", taskId: string, attempt: number): string | null => {
    const artifactDir = join(dir, PIPELINE, kind)
    const sealPath = join(artifactDir, `${taskId}-a${attempt}.seal`)
    if (!existsSync(sealPath)) return `нет пломбы ${PIPELINE}/${kind}/${taskId}-a${attempt}.seal`
    const seal = readJson<any>(sealPath, null)
    if (!seal || typeof seal.files !== "object" || seal.files === null) return "пломба повреждена"
    for (const [name, expected] of Object.entries(seal.files as Record<string, string>)) {
      const path = join(artifactDir, name)
      if (!existsSync(path)) return `файл ${name} из пломбы отсутствует`
      const actual = "sha256:" + createHash("sha256").update(readFileSync(path)).digest("hex")
      if (actual !== expected) return `файл ${name} изменён после пломбы`
    }
    return null
  }

  const latestVerdict = (taskId: string): { path: string; attempt: number } | null => {
    const artifactDir = join(dir, PIPELINE, "verdicts")
    if (!existsSync(artifactDir)) return null
    let best: { path: string; attempt: number } | null = null
    for (const name of readdirSync(artifactDir)) {
      const match = name.match(VERDICT_JSON)
      if (!match || match[1] !== taskId) continue
      const attempt = Number(match[2])
      if (!best || attempt > best.attempt) best = { path: join(artifactDir, name), attempt }
    }
    return best
  }

  const stagedHash = (): string => {
    const diff = execFileSync(
      "git",
      ["diff", "--cached", "--binary", "--no-color", "--", ".", ":(exclude).pipeline"],
      { cwd: dir, maxBuffer: 256 * 1024 * 1024 },
    )
    return "sha256:" + createHash("sha256").update(diff).digest("hex")
  }

  const terminalStatuses = new Set(["COMMITTED", "ESCALATED", "CANCELLED", "STOPPED"])
  const activeState = (): { path: string; state: any } | null => {
    const stateDir = join(dir, PIPELINE, "state")
    if (!existsSync(stateDir)) return null
    let best: { path: string; state: any } | null = null
    for (const name of readdirSync(stateDir)) {
      if (!name.endsWith(".json")) continue
      const path = join(stateDir, name)
      const state = readJson<any>(path, null)
      if (!state || terminalStatuses.has(String(state.status))) continue
      if (!best || String(state.updated_at ?? "") > String(best.state.updated_at ?? "")) best = { path, state }
    }
    return best
  }

  const recordCandidate = async () => {
    try {
      const active = activeState()
      if (!active) return
      await mutateState(active.path, (state) => {
        const hash = stagedHash()
        if (state.last_candidate_hash === hash) return
        state.attempts = Number(state.attempts ?? 0) + 1
        state.last_candidate_hash = hash
        state.dispatch_open = false
      })
    } catch {}
  }

  const applyReportMode = async (text: string) => {
    const mode = BRIEF_SIGNAL.test(text) ? "brief" : VERBOSE_SIGNAL.test(text) ? "verbose" : null
    if (!mode) return
    try {
      atomicWrite(join(dir, PIPELINE, "report-mode"), mode + "\n")
      const active = activeState()
      if (active) {
        await mutateState(active.path, (state) => {
          state.report_mode = mode
        })
      }
    } catch {}
  }

  const applyRouteHint = (text: string) => {
    let best: { route: string; index: number } | null = null
    for (const signal of ROUTE_SIGNALS) {
      const match = signal.re.exec(text)
      if (!match || match.index === undefined) continue
      if (!best || match.index > best.index) best = { route: signal.route, index: match.index }
    }
    if (!best) return
    try {
      atomicWrite(
        join(dir, PIPELINE, "route-hint"),
        JSON.stringify({ route: best.route, at: new Date().toISOString() }) + "\n",
      )
    } catch {}
  }

  const routeOf = (state: any): string => {
    const route = String(state?.route ?? "")
    if (route) return route
    return state?.design_required === false ? "standard" : "full"
  }

  const clearConsumedRouteHint = (args: any) => {
    const raw = String(args?.filePath ?? args?.path ?? args?.file ?? "")
    if (!raw) return
    const rel = relativeTo(dir, raw)
    if (!rel.startsWith(`${PIPELINE}/state/`) || !rel.endsWith(".json")) return
    const state = readJson<any>(join(dir, rel), null)
    if (!state || !ROUTE_VALUES.has(String(state.route ?? ""))) return
    const hint = join(dir, PIPELINE, "route-hint")
    if (!existsSync(hint)) return
    try {
      unlinkSync(hint)
    } catch {}
  }

  const isDispatchTool = (name: string) =>
    /(?:^|[._-])create[._-]?agent$/i.test(name) || /send[._-]?agent[._-]?prompt$/i.test(name)

  const dispatchTarget = (args: any): Role | undefined => {
    const modeId = String(args?.settings?.modeId ?? "").toLowerCase()
    if (isRole(modeId)) return modeId
    const prompt = String(args?.initialPrompt ?? args?.prompt ?? "")
    const marker = prompt.match(ROLE_MARKER)
    const role = marker?.[1]?.toLowerCase() ?? ""
    return isRole(role) ? role : undefined
  }

  const checkEdit = (role: Role, args: any) => {
    const raw = String(args?.filePath ?? args?.path ?? args?.file ?? "")
    if (!raw) return
    const rel = relativeTo(dir, raw)
    const name = rel.split("/").pop() ?? ""
    if (role === "lead" && !under(rel, PIPELINE)) deny(`лид не правит файлы вне ${PIPELINE}/: ${rel}`)
    if (role === "lead" && under(rel, `${PIPELINE}/verdicts`)) {
      deny("вердикты — неизменяемые доказательства; лид их создавать и править не может")
    }
    if (role === "lead" && under(rel, `${PIPELINE}/rework`)) {
      if (!REWORK_FILE.test(name)) deny("rework-пакет должен называться <task>-a<N>.md")
      if (existsSync(join(dir, rel))) deny("rework-пакет неизменяем после записи: новая попытка — новый номер")
    }
    if (role === "architect" && !under(rel, `${PIPELINE}/designs`)) deny("архитектор пишет только в .pipeline/designs/")
    if (role === "developer" && under(rel, PIPELINE)) deny("разработчику запрещено писать в .pipeline/")
    if (role === "reviewer") {
      if (!under(rel, `${PIPELINE}/verdicts`)) deny("ревьювер пишет только вердикты в .pipeline/verdicts/")
      if (!VERDICT_FILE.test(name)) deny("вердикт должен называться <task>-a<N>.md или <task>-a<N>.json")
      if (existsSync(join(dir, rel))) deny("вердикт уже зафиксирован: попытка неизменяема")
    }
  }

  const checkCommit = (command: string) => {
    if (COMMIT_BANNED.test(command)) deny("коммит с --amend/-a/--patch запрещён")
    const taskId = command.match(TASK_ID)?.[0]
    if (!taskId) {
      deny("коммит обязан содержать task_id (T-ГГГГММДД-NN) в сообщении")
    } else {
      const found = latestVerdict(taskId)
      if (!found) deny(`нет вердикта ${PIPELINE}/verdicts/<task>-a<N>.json — коммит запрещён`)
      const verdict = readJson<any>(found.path, null)
      if (!verdict) deny("вердикт повреждён — коммит запрещён")
      const sealIssue = verifySeal("verdicts", taskId, found.attempt)
      if (sealIssue) deny(`${sealIssue} — коммит запрещён`)
      const seal = readJson<any>(join(dir, PIPELINE, "verdicts", `${taskId}-a${found.attempt}.seal`), null)
      const files = seal?.files ?? {}
      const mdName = `${taskId}-a${found.attempt}.md`
      if (!(mdName in files)) deny(`вердикт ${mdName} не входит в пломбу — коммит запрещён`)
      if (verdict.verdict !== "PASS") deny(`вердикт ${verdict.verdict}: коммит запрещён`)
      const hash = stagedHash()
      if (verdict.candidate_hash !== hash) {
        deny(`PASS устарел: вердикт ${verdict.candidate_hash}, индекс ${hash} — нужна перепроверка`)
      }
    }
  }

  const checkBash = (role: Role, args: any) => {
    const command = String(args?.command ?? "")
    if (!command) return
    const subcommands = gitSubcommands(command)
    const nonRead = subcommands.filter((sub) => !GIT_READ_SUBCOMMANDS.has(sub))

    if (role === "lead") {
      const foreign = subcommands.filter((sub) => !GIT_LEAD_SUBCOMMANDS.has(sub) && sub !== "commit")
      if (foreign.length > 0) deny(`лиду разрешены git status/diff/log/show/add/commit, получено: ${foreign.join(", ")}`)
      if (subcommands.includes("commit")) {
        checkCommit(command)
        return
      }
      if (WRITE_SIGNAL.test(command) && /verdicts|rework/.test(command)) {
        deny("доказательства (verdicts/rework) неизменяемы — правка запрещена")
      }
      if (WRITE_SIGNAL.test(command) && !command.includes(PIPELINE)) deny("лид пишет файлы только в .pipeline/")
      return
    }

    if (nonRead.length > 0) deny(`git-мутации запрещены роли ${role}: ${nonRead.join(", ")}`)
    if ((role === "architect" || role === "reviewer") && WRITE_SIGNAL.test(command)) deny(`роль ${role} не выполняет мутирующие команды`)
    if (role === "developer" && command.includes(PIPELINE)) deny("разработчику запрещён доступ к .pipeline/ через bash")
    if (role === "reviewer" && command.includes(PIPELINE) && !command.includes("candidate-hash.sh")) {
      deny("ревьюверу в .pipeline/ разрешён только candidate-hash.sh")
    }
  }

  const checkDispatch = (args: any) => {
    const target: Role | undefined = dispatchTarget(args)
    if (target === undefined) {
      deny("диспетч обязан указывать роль: settings.modeId или ROLE-маркер")
    } else {
      const prompt = String(args?.initialPrompt ?? args?.prompt ?? "")
      const taskId = prompt.match(TASK_ID)?.[0]
      if (!taskId) {
        deny("диспетч обязан содержать task_id вида T-ГГГГММДД-NN")
      } else {
        if ((target === "developer" || target === "reviewer") && !existsSync(briefFile(taskId))) {
          deny(`нет брифа ${PIPELINE}/briefs/${taskId}.md — нет диспетча`)
        }
        const state = readJson<any>(stateFile(taskId), null)
        if ((target === "developer" || target === "reviewer") && !state) {
          deny(`нет состояния ${PIPELINE}/state/${taskId}.json`)
        }
        if (state && target === "reviewer" && state.dispatch_open === true) {
          deny("кандидат не зафиксирован: перед диспетчем ревьювера зафиксируй снимок через git add")
        }
        if (state && target === "architect") {
          const teamPath = join(dir, PIPELINE, "team.json")
          if (existsSync(teamPath)) {
            const team = readJson<any>(teamPath, null)
            const modules = Array.isArray(team?.modules) ? team.modules : []
            if (!modules.includes("architect")) deny("модуль architect не включён в beach-team.json")
          }
        }
        if (state && target === "developer") {
          const decision = String(state.infra_decision ?? "")
          if (decision === "stop") deny("человек остановил задачу после инфраструктурных отказов")
          const route = routeOf(state)
          if (!ROUTE_VALUES.has(route)) {
            deny(`недопустимый route «${route}» — допустимы: full, standard, assisted`)
          }
          if (route === "assisted") {
            deny("маршрут assisted: правку вносит человек — диспетч разработчика запрещён")
          }
          if (route === "full" && state.design_approved !== true) {
            deny("маршрут full требует утверждённого человеком дизайна")
          }
          if (state.roadmap_id) {
            const roadmapPath = join(dir, PIPELINE, "roadmap.json")
            if (existsSync(roadmapPath)) {
              const roadmap = readJson<any>(roadmapPath, null)
              const nodes: any[] = Array.isArray(roadmap?.nodes) ? roadmap.nodes : []
              const node = nodes.find((item) => item?.id === state.roadmap_id)
              if (!node) deny(`узел roadmap «${state.roadmap_id}» не найден в ${PIPELINE}/roadmap.json`)
              const deps: string[] = Array.isArray(node.depends_on) ? node.depends_on : []
              const undone = deps.filter((id) => {
                const dep = nodes.find((item) => item?.id === id)
                return !dep || String(dep.status) !== "done"
              })
              if (undone.length > 0) deny(`roadmap: не завершены зависимости ${undone.join(", ")} — диспетч запрещён`)
            }
          }
          const max = Number(state.max_attempts ?? 3)
          const used = Number(state.attempts ?? 0)
          if (used >= max) {
            deny(`бюджет попыток исчерпан (${used}/${max}; попытка = зафиксированный кандидат) — эскалация человеку`)
          }
          const maxInfra = Number(state.max_infra_failures ?? 4)
          const infra = Number(state.infra_failures ?? 0)
          const prospective = infra + (state.dispatch_open === true ? 1 : 0)
          if (prospective >= maxInfra && decision !== "continue" && decision !== "replace") {
            deny(`инфраструктурных отказов ${prospective} из ${maxInfra} — нужно решение человека: продолжить, заменить исполнителя или остановить`)
          }
        }
      }
    }
  }

  return {
    "chat.params": async (input) => {
      const role = input.agent?.toLowerCase() ?? ""
      if (isRole(role)) roles.set(input.sessionID, role)
    },
    "chat.message": async (input, output) => {
      const text = output.parts
        .map((part) => (part.type === "text" ? ((part as any).text ?? "") : ""))
        .join("\n")
      const agentRole = input.agent?.toLowerCase() ?? ""
      let role: Role | undefined
      if (isRole(agentRole)) {
        role = agentRole
      } else {
        const marker = text.match(ROLE_MARKER)
        const found = marker?.[1]?.toLowerCase() ?? ""
        if (isRole(found)) role = found
      }
      if (!role) return
      roles.set(input.sessionID, role)
      if (role === "lead") {
        await applyReportMode(text)
        applyRouteHint(text)
      }
    },
    "permission.ask": async (input, output) => {
      const role = roles.get(input.sessionID)
      if (role && output.status === "ask") output.status = "deny"
    },
    "tool.execute.before": async (input, output) => {
      const role = roles.get(input.sessionID)
      if (!role) return
      const args: any = output.args ?? {}
      if (MUTATING_TOOLS.has(input.tool)) {
        checkEdit(role, args)
        return
      }
      if (input.tool === "bash" || input.tool === "shell") {
        checkBash(role, args)
        return
      }
      if (role === "lead" && isDispatchTool(input.tool)) checkDispatch(args)
    },
    "tool.execute.after": async (input) => {
      const role = roles.get(input.sessionID)
      if (!role) return
      const args: any = input.args ?? {}
      if (input.tool === "bash" || input.tool === "shell") {
        if (role !== "lead") return
        const command = String(args?.command ?? "")
        if (/\bgit\s+add\b/.test(command)) await recordCandidate()
        return
      }
      if (MUTATING_TOOLS.has(input.tool)) {
        const raw = String(args?.filePath ?? args?.path ?? args?.file ?? "")
        if (!raw) return
        const rel = relativeTo(dir, raw)
        const name = rel.split("/").pop() ?? ""
        if (role === "reviewer" && rel.startsWith(`${PIPELINE}/verdicts/`)) {
          const match = name.match(VERDICT_JSON)
          if (match) sealAttempt("verdicts", match[1], Number(match[2]))
          return
        }
        if (role === "lead") {
          if (rel.startsWith(`${PIPELINE}/rework/`)) {
            const match = name.match(REWORK_FILE)
            if (match) sealAttempt("rework", match[1], Number(match[2]))
          }
          clearConsumedRouteHint(args)
        }
        return
      }
      if (role !== "lead" || !isDispatchTool(input.tool)) return
      const target: Role | undefined = dispatchTarget(args)
      if (target !== "developer") return
      const prompt = String(args?.initialPrompt ?? args?.prompt ?? "")
      const taskId = prompt.match(TASK_ID)?.[0]
      if (!taskId) return
      const path = stateFile(taskId)
      await mutateState(path, (state) => {
        if (state.dispatch_open === true) {
          state.infra_failures = Number(state.infra_failures ?? 0) + 1
        }
        state.dispatch_open = true
        const maxInfra = Number(state.max_infra_failures ?? 4)
        const decision = String(state.infra_decision ?? "")
        if (Number(state.infra_failures ?? 0) >= maxInfra && (decision === "continue" || decision === "replace")) {
          state.infra_decision_history = [
            ...(Array.isArray(state.infra_decision_history) ? state.infra_decision_history : []),
            { decision, at: new Date().toISOString() },
          ]
          state.infra_failures = 0
          state.infra_decision = null
        }
      })
    },
  }
}

export default plugin
