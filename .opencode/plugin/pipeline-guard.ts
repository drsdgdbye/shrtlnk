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
const GIT_LEAD_SUBCOMMANDS = new Set([
  "status",
  "diff",
  "log",
  "show",
  "add",
  "push",
  "checkout",
  "switch",
  "branch",
  "fetch",
  "pull",
  "ls-remote",
  "remote",
])
const BANNED_GIT_REMOTE = /\bgit\s+remote\s+(?!(-v|show)\b)\S/
const GIT_FLAGS_WITH_VALUE = new Set(["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"])
const COMMIT_BANNED = /(?:^|\s)(--amend|--all|-a|--patch|-p)(?=\s|$)/
const DEFAULT_ID_FORMAT = "DRS-YYMMDD-NN"
const ROLE_MARKER = /ROLE:\s*(lead|architect|developer|reviewer)\b/i
const VERBOSE_SIGNAL = /(\/verbose\b|verbose|in more detail|in detail|more details|in simple terms|simply|for a beginner|eli5|break it down)/i
const BRIEF_SIGNAL = /(\/brief\b|brief|shorter|no details|normal mode|keep it brief)/i
const ROUTE_VALUES = new Set(["full", "standard", "assisted", "release"])
const ROUTE_SIGNALS: Array<{ route: string; re: RegExp }> = [
  { route: "full", re: /(\/full\b|full route|with design)/i },
  { route: "standard", re: /(\/standard\b|skip the architect|without the architect|without design|shortened route)/i },
  { route: "assisted", re: /(\/assisted\b|i\'?ll fix it myself|i\'?ll do it myself|i\'?ll patch it myself|without the developer|fix it myself)/i },
]
const WRITE_SIGNAL = /\bsed\s+-i|\brm\s|\bmv\s|\bcp\s|\btee\b|\bdd\s|\btruncate\b|>>?\s*(?!\/dev\/null)[^&\s]/
function regexFromIdFormat(idFormat: string): string {
  let pattern = idFormat
  for (const [token, re] of [["YYYYMMDD", "\\d{8}"], ["YYMMDD", "\\d{6}"], ["NN", "\\d{2}"], ["N", "\\d+"]] as const) {
    pattern = pattern.split(token).join(re)
  }
  return pattern
}

const APPROVAL_OPS = ["repo", "bootstrap", "commit", "push", "pr", "merge", "release"] as const
type ApprovalOp = (typeof APPROVAL_OPS)[number]
const APPROVAL_SIGNALS: Array<{ op: ApprovalOp; re: RegExp }> = [
  { op: "repo", re: /(\/approve\s+repo\b|i approve creating the repo(?:sitory)?|create the repo(?:sitory)?|you can create the repo)/i },
  { op: "bootstrap", re: /(\/approve\s+bootstrap\b|i approve the repository setup|set up the repository)/i },
  { op: "commit", re: /(\/approve\s+commit\b|i approve the commit|you can commit|commit it)/i },
  { op: "push", re: /(\/approve\s+push\b|i approve the push|you can push|push it)/i },
  { op: "pr", re: /(\/approve\s+pr\b|i approve the pr|create the pr|open a pr|you can (?:open|create) a pr)/i },
  { op: "merge", re: /(\/approve\s+merge\b|i approve the merge|merge it|you can merge)/i },
  { op: "release", re: /(\/approve\s+release\b|i approve the release|cut a release|you can release)/i },
]
const REVOKE_SIGNALS: Array<{ op: ApprovalOp; re: RegExp }> = [
  { op: "repo", re: /(\/revoke\s+repo\b|i revoke the repo(?:sitory)?(?: creation)?)/i },
  { op: "bootstrap", re: /(\/revoke\s+bootstrap\b|i revoke the repository setup)/i },
  { op: "commit", re: /(\/revoke\s+commit\b|i revoke the commit)/i },
  { op: "push", re: /(\/revoke\s+push\b|i revoke the push)/i },
  { op: "pr", re: /(\/revoke\s+pr\b|i revoke the pr)/i },
  { op: "merge", re: /(\/revoke\s+merge\b|i revoke the merge)/i },
  { op: "release", re: /(\/revoke\s+release\b|i revoke the release)/i },
]
const UMBRELLA_RE = /(\/approve\s+delivery\b|i approve the delivery|full delivery cycle)/i

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
  const parseJsonFile = (path: string): any => {
    try {
      return JSON.parse(readFileSync(path, "utf8"))
    } catch {
      return null
    }
  }
  const teamConfig =
    parseJsonFile(join(dir, PIPELINE, "team.json")) ?? parseJsonFile(join(dir, "beach-team.json"))
  const gitCfg = {
    model: String(teamConfig?.git?.model ?? "trunk"),
    main: String(teamConfig?.git?.main ?? "main"),
    dev: String(teamConfig?.git?.dev ?? "dev"),
  }
  const idFormat = String(teamConfig?.tasks?.id_format ?? DEFAULT_ID_FORMAT)
  const idPattern = String(teamConfig?.tasks?.id_pattern ?? regexFromIdFormat(idFormat))
  const branchPrefixes: Record<string, string> = {
    feat: String(teamConfig?.tasks?.branch_prefixes?.feat ?? "feat"),
    fix: String(teamConfig?.tasks?.branch_prefixes?.fix ?? "fix"),
    chore: String(teamConfig?.tasks?.branch_prefixes?.chore ?? "chore"),
    hotfix: String(teamConfig?.tasks?.branch_prefixes?.hotfix ?? "hotfix"),
    release: String(teamConfig?.tasks?.branch_prefixes?.release ?? "release"),
  }
  const TASK_ID = new RegExp(`\\b${idPattern}\\b`)
  const VERDICT_FILE = new RegExp(`^${idPattern}-a\\d+\\.(md|json)$`)
  const VERDICT_JSON = new RegExp(`^(${idPattern})-a(\\d+)\\.json$`)
  const REWORK_FILE = new RegExp(`^(${idPattern})-a(\\d+)\\.md$`)

  const releaseTag = (state: any): string => {
    const version = String(state?.version ?? "").trim()
    if (!version) return ""
    return version.startsWith("v") ? version : `v${version}`
  }

  const expectedBranch = (state: any): string => {
    const type = String(state?.type ?? "")
    if (type === "release") return `${branchPrefixes.release}/${releaseTag(state)}`
    const prefix = branchPrefixes[type] ?? type
    return prefix ? `${prefix}/${String(state?.task_id ?? "")}` : ""
  }

  const expectedPrBase = (type: string): string => {
    if (gitCfg.model !== "simple-gitflow") return gitCfg.main
    return type === "hotfix" || type === "release" ? gitCfg.main : gitCfg.dev
  }

  const commandArg = (command: string, flag: string): string => {
    const match = command.match(new RegExp(`(?:^|\\s)${flag}\\s+("[^"]*"|'[^']*'|\\S+)`))
    return match ? match[1].replace(/^["']|["']$/g, "") : ""
  }
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
    if (!existsSync(sealPath)) return `missing seal ${PIPELINE}/${kind}/${taskId}-a${attempt}.seal`
    const seal = readJson<any>(sealPath, null)
    if (!seal || typeof seal.files !== "object" || seal.files === null) return "seal is corrupted"
    for (const [name, expected] of Object.entries(seal.files as Record<string, string>)) {
      const path = join(artifactDir, name)
      if (!existsSync(path)) return `file ${name} from the seal is missing`
      const actual = "sha256:" + createHash("sha256").update(readFileSync(path)).digest("hex")
      if (actual !== expected) return `file ${name} changed after sealing`
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

  const gitOut = (args: string[]): string => {
    try {
      return execFileSync("git", args, { cwd: dir, stdio: "pipe" }).toString("utf8").trim()
    } catch {
      return ""
    }
  }

  const currentBranch = () => gitOut(["rev-parse", "--abbrev-ref", "HEAD"])
  const headCommit = () => gitOut(["rev-parse", "HEAD"])

  const approvalPath = (key: string) => join(dir, PIPELINE, "approvals", `${key}.json`)

  const requireApproval = (key: string, op: ApprovalOp, what: string) => {
    const data = readJson<any>(approvalPath(key), null)
    if (!data || typeof data[op] !== "string" || !data[op]) {
      deny(`no human approval for ${what} — send "I approve the ${op}" or /approve ${op}`)
    }
  }

  const applyApprovals = (text: string) => {
    try {
      const active = activeState()
      const taskId = active ? String(active.state.task_id ?? "") : ""
      const write = (key: string, mutate: (data: any) => void) => {
        const path = approvalPath(key)
        const data = readJson<any>(path, {}) ?? {}
        mutate(data)
        data.updated_at = new Date().toISOString()
        writeJson(path, data)
      }
      for (const signal of APPROVAL_SIGNALS) {
        if (!signal.re.test(text)) continue
        const key = signal.op === "repo" || signal.op === "bootstrap" ? "_repo" : taskId || "_general"
        write(key, (data) => {
          data[signal.op] = new Date().toISOString()
        })
      }
      if (UMBRELLA_RE.test(text)) {
        const key = taskId || "_general"
        write(key, (data) => {
          const at = new Date().toISOString()
          data.commit = data.commit ?? at
          data.push = data.push ?? at
          data.pr = data.pr ?? at
        })
      }
      for (const signal of REVOKE_SIGNALS) {
        if (!signal.re.test(text)) continue
        const key = signal.op === "repo" || signal.op === "bootstrap" ? "_repo" : taskId || "_general"
        write(key, (data) => {
          delete data[signal.op]
        })
      }
    } catch {}
  }

  const checkPush = (command: string) => {
    const active = activeState()
    const branch = currentBranch()
    if (!active) {
      const bootstrap = new RegExp(
        `^git\\s+push\\s+(?:-u|--set-upstream)\\s+origin\\s+(?:${gitCfg.main}|${gitCfg.dev})\\s*$`,
      )
      if (!bootstrap.test(command.trim())) {
        deny(`no active task — push is forbidden (bootstrap allows only "git push -u origin ${gitCfg.main}|${gitCfg.dev}")`)
      }
      requireApproval("_repo", "bootstrap", "repository setup")
      return
    }
    const taskId = String(active!.state.task_id ?? "")
    const expected = expectedBranch(active!.state)
    if (!expected) deny("cannot derive the task branch: set state.type and, for release, state.version")
    if (branch !== expected) deny(`push is only allowed from branch ${expected}, current: ${branch || "unknown"}`)
    requireApproval(taskId, "push", "push")
    const head = headCommit()
    if (!head || head !== active!.state.delivered_commit) {
      deny("push only of a verified commit: HEAD does not match delivered_commit")
    }
  }

  const checkGh = (command: string) => {
    const active = activeState()
    const taskId = active ? String(active.state.task_id ?? "") : ""
    if (/\bgh\s+repo\s+create\b/.test(command)) {
      requireApproval("_repo", "repo", "repository creation")
      return
    }
    if (/\bgh\s+pr\s+create\b/.test(command)) {
      if (!active) deny("no active task for creating a PR")
      const type = String(active!.state.type ?? "")
      const base = commandArg(command, "--base")
      if (type === "release") {
        const tag = releaseTag(active!.state)
        if (!tag) deny("release task has no version: set state.version")
        const backmerge = Boolean(active!.state.pr_number)
        const expectedBase = backmerge ? gitCfg.dev : gitCfg.main
        if (!base) deny(`PR must specify --base ${expectedBase}`)
        if (base !== expectedBase) deny(`release PR base must be ${expectedBase}, got "${base}"`)
        const headArg = commandArg(command, "--head")
        const head = !headArg || headArg === "HEAD" ? currentBranch() : headArg
        const allowed = backmerge ? [gitCfg.main] : [`${branchPrefixes.release}/${tag}`]
        if (!allowed.includes(head)) {
          deny(`release PR head must be ${allowed.join(" or ")}, got "${head}"`)
        }
        requireApproval(taskId, "pr", "PR creation")
        return
      }
      const expectedBase = expectedPrBase(type)
      if (!base) deny(`PR must specify --base ${expectedBase}`)
      if (base !== expectedBase) deny(`PR base must be ${expectedBase} for type "${type}", got "${base}"`)
      requireApproval(taskId, "pr", "PR creation")
      const head = headCommit()
      if (!head || head !== active!.state.delivered_commit) {
        deny("PR only for a verified commit: HEAD does not match delivered_commit")
      }
      return
    }
    if (/\bgh\s+pr\s+merge\b/.test(command)) {
      if (!active) deny("no active task for merge")
      requireApproval(taskId, "merge", "merge")
      const type = String(active!.state.type ?? "")
      if (type === "release") {
        if (!active!.state.pr_number) deny("release merge only for a PR created through the pipeline")
        if (active!.state.merge_commit && active!.state.backmerge_at) deny("release task already finished both merges")
        return
      }
      if (!active!.state.pr_number) deny("merge only for a PR created through the pipeline")
      return
    }
    if (/\bgh\s+release\s+create\b/.test(command)) {
      if (!active) deny("gh release create requires an active release task")
      const type = String(active!.state.type ?? "")
      if (type !== "release") deny("gh release create is only allowed for release tasks")
      requireApproval(taskId || "_general", "release", "release")
      if (!active!.state.merge_commit) deny("release only after merge into main")
      const tag = releaseTag(active!.state)
      if (tag && !command.includes(tag)) deny(`release tag must match the task version ${tag}`)
      return
    }
    if (/\bgh\s+workflow\s+run\b/.test(command)) deny("gh workflow run is outside the mandate")
    if (/\bgh\s+api\b/.test(command) && /(-X|--method)\s*(POST|PUT|PATCH|DELETE)|(^|\s)-f(\s|$)|mutation/i.test(command)) {
      deny("mutating gh api is forbidden")
    }
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

  const recordCommit = async (command: string) => {
    const active = activeState()
    if (!active) return
    const head = headCommit()
    if (!head) return
    await mutateState(active.path, (state) => {
      const taskId = command.match(TASK_ID)?.[0]
      if (taskId && taskId !== state.task_id) return
      state.delivered_commit = head
    })
  }

  const recordPullRequest = (output: unknown) => {
    const active = activeState()
    if (!active) return
    const text = JSON.stringify(output ?? {})
    const match = text.match(/(https:\/\/github\.com\/[^"\s]+\/pull\/(\d+))/)
    if (!match) return
    const state = readJson<any>(active.path, null)
    if (!state) return
    if (String(state.type) === "release" && state.pr_number) {
      state.backmerge_pr = Number(match[2])
      state.backmerge_pr_url = match[1]
    } else {
      state.pr_url = match[1]
      state.pr_number = Number(match[2])
    }
    state.updated_at = new Date().toISOString()
    writeJson(active.path, state)
  }

  const recordMerge = async () => {
    const active = activeState()
    if (!active) return
    const pr = Number(active.state.pr_number ?? 0)
    let mergeCommit = ""
    if (pr > 0) {
      try {
        mergeCommit = execFileSync(
          "gh",
          ["pr", "view", String(pr), "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
          { cwd: dir, stdio: "pipe" },
        )
          .toString("utf8")
          .trim()
      } catch {}
    }
    await mutateState(active.path, (state) => {
      if (state.merge_commit) {
        state.backmerge_at = new Date().toISOString()
        return
      }
      state.merge_commit = mergeCommit || "merged"
      state.merged_at = new Date().toISOString()
    })
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
    if (role === "lead" && !under(rel, PIPELINE)) deny(`lead does not edit files outside ${PIPELINE}/: ${rel}`)
    if (role === "lead" && under(rel, `${PIPELINE}/verdicts`)) {
      deny("verdicts are immutable evidence; the lead cannot create or edit them")
    }
    if (role === "lead" && under(rel, `${PIPELINE}/approvals`)) {
      deny("only the plugin writes human approvals — the lead must not edit them")
    }
    if (role === "lead" && under(rel, `${PIPELINE}/rework`)) {
      if (!REWORK_FILE.test(name)) deny("rework packet must be named <task>-a<N>.md")
      if (existsSync(join(dir, rel))) deny("rework packet is immutable after it is written: a new attempt uses a new number")
    }
    if (role === "architect" && !under(rel, `${PIPELINE}/designs`)) deny("architect writes only to .pipeline/designs/")
    if (role === "developer" && under(rel, PIPELINE)) deny("the developer must not write to .pipeline/")
    if (role === "reviewer") {
      if (!under(rel, `${PIPELINE}/verdicts`)) deny("reviewer writes only verdicts to .pipeline/verdicts/")
      if (!VERDICT_FILE.test(name)) deny("verdict must be named <task>-a<N>.md or <task>-a<N>.json")
      if (existsSync(join(dir, rel))) deny("verdict is already recorded: an attempt is immutable")
    }
  }

  const checkCommit = (command: string) => {
    if (COMMIT_BANNED.test(command)) deny("commit with --amend/-a/--patch is forbidden")
    const taskId = command.match(TASK_ID)?.[0]
    if (!taskId) {
      deny(`commit message must contain task_id (${idFormat})`)
    } else {
      const found = latestVerdict(taskId)
      if (!found) deny(`no verdict ${PIPELINE}/verdicts/<task>-a<N>.json — commit is forbidden`)
      requireApproval(taskId, "commit", "commit")
      const verdict = readJson<any>(found.path, null)
      if (!verdict) deny("verdict is corrupted — commit is forbidden")
      const sealIssue = verifySeal("verdicts", taskId, found.attempt)
      if (sealIssue) deny(`${sealIssue} — commit is forbidden`)
      const seal = readJson<any>(join(dir, PIPELINE, "verdicts", `${taskId}-a${found.attempt}.seal`), null)
      const files = seal?.files ?? {}
      const mdName = `${taskId}-a${found.attempt}.md`
      if (!(mdName in files)) deny(`verdict ${mdName} is not covered by the seal — commit is forbidden`)
      if (verdict.verdict !== "PASS") deny(`verdict ${verdict.verdict}: commit is forbidden`)
      const hash = stagedHash()
      if (verdict.candidate_hash !== hash) {
        deny(`PASS is stale: verdict ${verdict.candidate_hash}, index ${hash} — re-verification required`)
      }
    }
  }

  const checkBash = (role: Role, args: any) => {
    const command = String(args?.command ?? "")
    if (!command) return
    const subcommands = gitSubcommands(command)
    const nonRead = subcommands.filter((sub) => !GIT_READ_SUBCOMMANDS.has(sub))

    if (role === "lead") {
      if (BANNED_GIT_REMOTE.test(command)) deny("git remote is read-only: `git remote -v` or `git remote show`")
      const foreign = subcommands.filter((sub) => !GIT_LEAD_SUBCOMMANDS.has(sub) && sub !== "commit")
      if (foreign.length > 0) {
        deny(`lead may run git status/diff/log/show/add/commit/push and branch operations, got: ${foreign.join(", ")}`)
      }
      if (/\bgh\s+/.test(command)) checkGh(command)
      if (subcommands.includes("commit")) {
        checkCommit(command)
        return
      }
      if (subcommands.includes("push")) checkPush(command)
      if (WRITE_SIGNAL.test(command) && /verdicts|rework/.test(command)) {
        deny("evidence (verdicts/rework) is immutable — editing is forbidden")
      }
      if (WRITE_SIGNAL.test(command) && !command.includes(PIPELINE)) deny("lead writes files only to .pipeline/")
      return
    }

    if (/\bgh\s+/.test(command)) deny(`role ${role} does not manage GitHub`)
    if (nonRead.length > 0) deny(`git mutations are forbidden for role ${role}: ${nonRead.join(", ")}`)
    if ((role === "architect" || role === "reviewer") && WRITE_SIGNAL.test(command)) deny(`role ${role} must not run mutating commands`)
    if (role === "developer" && command.includes(PIPELINE)) deny("the developer must not access .pipeline/ via bash")
    if (role === "reviewer" && command.includes(PIPELINE) && !command.includes("candidate-hash.sh")) {
      deny("in .pipeline/ the reviewer may only run candidate-hash.sh")
    }
  }

  const checkDispatch = (args: any) => {
    const target: Role | undefined = dispatchTarget(args)
    if (target === undefined) {
      deny("dispatch must specify a role: settings.modeId or the ROLE marker")
    } else {
      const prompt = String(args?.initialPrompt ?? args?.prompt ?? "")
      const taskId = prompt.match(TASK_ID)?.[0]
      if (!taskId) {
        deny(`dispatch must contain a task_id matching ${idFormat}`)
      } else {
        if ((target === "developer" || target === "reviewer") && !existsSync(briefFile(taskId))) {
          deny(`no brief ${PIPELINE}/briefs/${taskId}.md — no dispatch`)
        }
        const state = readJson<any>(stateFile(taskId), null)
        if ((target === "developer" || target === "reviewer") && !state) {
          deny(`no state ${PIPELINE}/state/${taskId}.json`)
        }
        if (state && target === "reviewer" && String(state.type ?? "") === "release") {
          deny("release tasks do not dispatch a reviewer — the lead drives the release flow")
        }
        if (state && target === "reviewer" && state.dispatch_open === true) {
          deny("candidate is not frozen: before dispatching the reviewer, freeze the snapshot with git add")
        }
        if (state && target === "architect") {
          const teamPath = join(dir, PIPELINE, "team.json")
          if (existsSync(teamPath)) {
            const team = readJson<any>(teamPath, null)
            const modules = Array.isArray(team?.modules) ? team.modules : []
            if (!modules.includes("architect")) deny("module architect is not enabled in beach-team.json")
          }
        }
        if (state && target === "developer") {
          const decision = String(state.infra_decision ?? "")
          if (decision === "stop") deny("the human stopped the task after infrastructure failures")
          const route = routeOf(state)
          if (!ROUTE_VALUES.has(route)) {
            deny(`invalid route "${route}" — allowed: full, standard, assisted, release`)
          }
          if (route === "release" || String(state.type) === "release") {
            deny("release tasks do not dispatch a developer or a reviewer — the lead drives the release flow")
          }
          if (route === "assisted") {
            deny("route assisted: the human makes the change — developer dispatch is forbidden")
          }
          if (route === "full" && state.design_approved !== true) {
            deny("route full requires a human-approved design")
          }
          if (state.roadmap_id) {
            const roadmapPath = join(dir, PIPELINE, "roadmap.json")
            if (existsSync(roadmapPath)) {
              const roadmap = readJson<any>(roadmapPath, null)
              const nodes: any[] = Array.isArray(roadmap?.nodes) ? roadmap.nodes : []
              const node = nodes.find((item) => item?.id === state.roadmap_id)
              if (!node) deny(`roadmap node "${state.roadmap_id}" not found in ${PIPELINE}/roadmap.json`)
              const deps: string[] = Array.isArray(node.depends_on) ? node.depends_on : []
              const undone = deps.filter((id) => {
                const dep = nodes.find((item) => item?.id === id)
                return !dep || String(dep.status) !== "done"
              })
              if (undone.length > 0) deny(`roadmap: unfinished dependencies ${undone.join(", ")} — dispatch is forbidden`)
            }
          }
          const max = Number(state.max_attempts ?? 3)
          const used = Number(state.attempts ?? 0)
          if (used >= max) {
            deny(`attempt budget exhausted (${used}/${max}; an attempt = a frozen candidate) — escalate to the human`)
          }
          const maxInfra = Number(state.max_infra_failures ?? 4)
          const infra = Number(state.infra_failures ?? 0)
          const prospective = infra + (state.dispatch_open === true ? 1 : 0)
          if (prospective >= maxInfra && decision !== "continue" && decision !== "replace") {
            deny(`infrastructure failures ${prospective} of ${maxInfra} — a human decision is required: continue, replace the executor, or stop`)
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
        applyApprovals(text)
      }
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
    "tool.execute.after": async (input, output) => {
      const role = roles.get(input.sessionID)
      if (!role) return
      const args: any = input.args ?? {}
      if (input.tool === "bash" || input.tool === "shell") {
        if (role !== "lead") return
        const command = String(args?.command ?? "")
        if (/\bgit\s+add\b/.test(command)) await recordCandidate()
        if (/\bgit\s+commit\b/.test(command)) await recordCommit(command)
        if (/\bgh\s+pr\s+create\b/.test(command)) recordPullRequest(output)
        if (/\bgh\s+pr\s+merge\b/.test(command)) await recordMerge()
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
