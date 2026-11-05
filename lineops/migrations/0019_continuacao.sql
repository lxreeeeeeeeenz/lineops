-- Continuações informadas: depois do voo X, o avião (e a tripulação) segue no voo Y. weekdays: dígitos 0 (seg) a 6 (dom); vazio = todos.
CREATE TABLE continuation_rule (
    id INTEGER PRIMARY KEY,
    airline TEXT NOT NULL,
    from_ident TEXT NOT NULL,
    to_ident TEXT NOT NULL,
    weekdays TEXT NOT NULL DEFAULT '',
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_cont_rule_from ON continuation_rule(airline, from_ident);
