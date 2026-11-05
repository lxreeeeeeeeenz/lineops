-- 3.0.2b6: biblioteca técnica (PDF importados por você, página a página) e a MEL item a item
CREATE TABLE IF NOT EXISTS library_doc (
    id INTEGER PRIMARY KEY,
    airline TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    fleet TEXT,
    revision TEXT,
    rev_date TEXT,
    file_name TEXT,
    pages INTEGER NOT NULL DEFAULT 0,
    sha1 TEXT,
    imported_at TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS library_page (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES library_doc(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_library_page_doc ON library_page(doc_id, page);
CREATE TABLE IF NOT EXISTS mel_item (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES library_doc(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    ata TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    parent TEXT,
    section TEXT,
    title TEXT NOT NULL DEFAULT '',
    category TEXT,
    installed INTEGER,
    required INTEGER,
    remarks TEXT NOT NULL DEFAULT '',
    maint TEXT NOT NULL DEFAULT '',
    oper TEXT NOT NULL DEFAULT '',
    placard TEXT NOT NULL DEFAULT '',
    page INTEGER
);
CREATE INDEX IF NOT EXISTS ix_mel_item_doc ON mel_item(doc_id, code);
CREATE INDEX IF NOT EXISTS ix_mel_item_ata ON mel_item(ata);
ALTER TABLE discrepancy ADD COLUMN mel_item_id INTEGER REFERENCES mel_item(id);
ALTER TABLE discrepancy ADD COLUMN mel_category TEXT;
