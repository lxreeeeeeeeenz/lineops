"""Biblioteca técnica: a MEL (formato AMOS) lida item a item, a busca por página e a discrepância ligada ao item."""
from lineops import biblioteca as B, db, fleet

PAGE = """ATA 21                                     Minimum Equipment List
Page 17                                     (Continued)AIR CONDITIONING

 System &                                      Item (1)     (2) Category
 Sequence                                                          (3) Number Installed
 numbers                                                                 (4) Number Required for Dispatch
 21     AIR CONDITIONING                                                        (5) Remarks or Exceptions
 10          Cabin Rate of Climb Indicator
  02         Digital Control System                         C   1   0          (P)May be inoperative provided AUTO and ALTN control modes
             (-300/-400/-500/-600/                                                operate normally.
             -700/-800/-900ER)
    03       Alternate Mode                                 B   2   1          (P)(M)(O)
                                                                                  May be inoperative provided:
                                                                                  a) Flight is unpressurized.

21-10-02 Digital Control System (-300/-400/-500/-600/ -700/-800/-900ER)

Maintenance Procedures
 PLACARD                                                                           PLACARD LOCATION
 INOP                                                                              Install next to the Cabin Rate of Climb Indicator

21-10-02-03 Alternate Mode

Maintenance Procedures
 PLACARD                                                                           PLACARD LOCATION
 INOP                                                                              Install next to the selector
Open and collar P6-4 circuit breaker.
Operating Procedures
1. Limit altitude to 10,000 ft.
                                                                                                                     19.January 2023
"""


def test_mel_items_from_layout_text():
    items = {i["code"]: i for i in B.parse_mel(["MINIMUM EQUIPMENT LIST\nMEL GOL B737-700/-800 NG\nRevision 28 (19.Jan.2023)", PAGE])}
    assert set(items) == {"21-10", "21-10-02", "21-10-02-03"}
    a = items["21-10-02"]
    assert a["title"] == "Digital Control System (-300/-400/-500/-600/ -700/-800/-900ER)" and a["category"] == "C"
    assert (a["installed"], a["required"]) == (1, 0) and a["remarks"].endswith("operate normally.") and a["section"] == "AIR CONDITIONING"
    assert "Install next to the Cabin Rate" in a["placard"] and not a["oper"]
    b = items["21-10-02-03"]
    assert b["parent"] == "21-10-02" and b["category"] == "B" and "a) Flight is unpressurized." in b["remarks"]
    assert "collar P6-4" in b["maint"] and b["oper"].startswith("1. Limit altitude") and "19.January" not in b["oper"]
    assert items["21-10"]["category"] is None and items["21-10"]["title"] == "Cabin Rate of Climb Indicator"
    assert B.due_date_for("C", "2026-01-26T10:00:00Z") == "2026-02-05" and B.due_date_for("A", "2026-01-26") is None


def test_search_and_discrepancy_from_mel(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "GLO.db")
    db.migrate(c)
    monkeypatch.setattr(B, "page_texts", lambda path, progress=None: ["MINIMUM EQUIPMENT LIST\nMEL GOL B737-700/-800 NG\nRevision 28 (19.Jan.2023)", PAGE])
    f = tmp_path / "mel.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    r = B.import_pdf(c, "GLO", f, "mel.pdf")
    assert r["kind"] == "MEL" and r["items"] == 3 and r["revision"] == "28" and r["pages"] == 2
    assert B.search(c, "GLO", "control modes")["items"][0]["page"] == 2
    assert B.search(c, "TAM", "control")["items"] == []                              # outra companhia não vê
    it = B.mel_search(c, "GLO", "21-10-02-03")["items"][0]
    aid = fleet.add_aircraft(c, registration="PR-GXA", airline="GLO", type_icao="B738")
    aid = aid["id"] if isinstance(aid, dict) else aid
    did = fleet.add_discrepancy(c, aid, "", mel_item_id=it["id"], airline="GLO")
    d = c.execute("SELECT * FROM discrepancy WHERE id=?", (did,)).fetchone()
    assert d["title"].startswith("21-10-02-03") and d["mel_category"] == "B" and d["status"] == "DIFERIDA"
    assert d["restriction"] == "INFORMA" and "Limit altitude" in d["restriction_text"] and d["due_date"] > d["opened_at"][:10]
    B.delete_doc(c, "GLO", r["id"])
    assert not B.docs(c, "GLO")
    c.close()
