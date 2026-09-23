CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mtime REAL NOT NULL
);

CREATE TABLE code_units (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    body_node_count INTEGER NOT NULL,
    body_hash TEXT NOT NULL
);

INSERT INTO files (id, path, mtime) VALUES
    (1, 'core/a.py', 1.0),
    (2, 'core/b.py', 1.0),
    (3, 'core/c.py', 1.0),
    (4, 'core/d.py', 1.0);

INSERT INTO code_units (
    id,
    file_id,
    name,
    body,
    start_line,
    end_line,
    body_node_count,
    body_hash
) VALUES
    (1, 1, 'render', 'def render():\n    return 1', 10, 11, 12, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'),
    (2, 2, 'build', 'def build():\n    return 2', 20, 21, 12, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'),
    (3, 3, 'same', 'def same():\n    return 3', 30, 31, 12, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'),
    (4, 4, 'same_copy', 'def same_copy():\n    return 3', 40, 41, 12, 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc');
