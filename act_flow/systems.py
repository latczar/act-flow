"""Stand-ins for ACT's business systems and the public registers.

Each mock system keeps its own SQLite file and its own field names (the CRM, the
Xero-style accounting system and the payroll product all describe a client
differently), so the adapters have real mapping work to do. Faults can be injected
to show retries, circuit breaking and resuming a half-finished case.
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import config

LABELS = {
    "crm": "CRM", "accounting": "Accounting system", "tax": "Tax system",
    "payroll": "Payroll system", "dms": "Document management system",
}


class TransientError(Exception):
    """The system did not respond. Safe to retry."""


class PermanentError(Exception):
    """The system rejected the request. Retrying will not help."""


class MockSystem:
    def __init__(self, name: str):
        self.name = name
        self.label = LABELS[name]
        self.path = config.sub("systems") / f"{name}.db"
        con = self._db()
        con.executescript(
            "CREATE TABLE IF NOT EXISTS records(collection TEXT, id TEXT, data TEXT, PRIMARY KEY(collection, id));"
            "CREATE TABLE IF NOT EXISTS faults(id INTEGER PRIMARY KEY CHECK(id = 1), kind TEXT, remaining INTEGER);")
        con.close()

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def _gate(self, con) -> None:
        row = con.execute("SELECT kind, remaining FROM faults WHERE id=1").fetchone()
        if row and row["remaining"] > 0:
            con.execute("UPDATE faults SET remaining = remaining - 1 WHERE id=1")
            con.commit()
            if row["kind"] == "permanent":
                raise PermanentError(f"{self.label} returned a validation error (simulated)")
            raise TransientError(f"{self.label} did not respond (simulated outage)")

    # -- API calls (each one can fail if a fault is injected) --
    def get(self, collection: str, rec_id: str) -> dict | None:
        con = self._db()
        try:
            self._gate(con)
            row = con.execute("SELECT data FROM records WHERE collection=? AND id=?", (collection, rec_id)).fetchone()
            return json.loads(row["data"]) if row else None
        finally:
            con.close()

    def find(self, collection: str, **match) -> list[dict]:
        con = self._db()
        try:
            self._gate(con)
            rows = con.execute("SELECT data FROM records WHERE collection=? ORDER BY id", (collection,)).fetchall()
        finally:
            con.close()
        items = [json.loads(r["data"]) for r in rows]
        return [i for i in items if all(str(i.get(k, "")).lower() == str(v).lower() for k, v in match.items())]

    def put(self, collection: str, rec_id: str, data: dict) -> None:
        con = self._db()
        try:
            self._gate(con)
            con.execute("INSERT INTO records(collection, id, data) VALUES(?,?,?) "
                        "ON CONFLICT(collection, id) DO UPDATE SET data=excluded.data",
                        (collection, rec_id, json.dumps(data, sort_keys=True)))
            con.commit()
        finally:
            con.close()

    def delete(self, collection: str, rec_id: str) -> None:
        con = self._db()
        try:
            self._gate(con)
            con.execute("DELETE FROM records WHERE collection=? AND id=?", (collection, rec_id))
            con.commit()
        finally:
            con.close()

    # -- admin helpers (not gated; used by the console and the demo) --
    def dump(self) -> dict[str, list[dict]]:
        con = self._db()
        try:
            rows = con.execute("SELECT collection, id, data FROM records ORDER BY collection, id").fetchall()
        finally:
            con.close()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["collection"], []).append({"_id": r["id"], **json.loads(r["data"])})
        return out

    def inject_fault(self, kind: str, count: int) -> None:
        con = self._db()
        con.execute("INSERT INTO faults(id, kind, remaining) VALUES(1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, remaining=excluded.remaining", (kind, count))
        con.commit()
        con.close()

    def clear_faults(self) -> None:
        self.inject_fault("transient", 0)

    def fault_status(self) -> dict:
        con = self._db()
        row = con.execute("SELECT kind, remaining FROM faults WHERE id=1").fetchone()
        con.close()
        return {"kind": row["kind"], "remaining": row["remaining"]} if row else {"kind": None, "remaining": 0}


def system(name: str) -> MockSystem:
    return MockSystem(name)


def all_systems() -> list[MockSystem]:
    return [system(n) for n in LABELS]


# ---- public registers (mocked) -------------------------------------------------

def _reference(name: str) -> dict:
    path = config.sub("reference") / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def companies_house_lookup(company_number: str) -> dict | None:
    """Stands in for the Companies House public data API."""
    return _reference("companies_house").get(company_number)


def vat_check_digits_ok(vat: str) -> bool:
    digits = re.sub(r"\D", "", vat.upper().removeprefix("GB"))
    if len(digits) != 9:
        return False
    total = sum(int(digits[i]) * w for i, w in enumerate(range(8, 1, -1))) + int(digits[7:])
    return total % 97 == 0 or (total + 55) % 97 == 0


def hmrc_vat_lookup(vat: str) -> str | None:
    """Stands in for HMRC's 'check a UK VAT number' API: returns the registered name."""
    if not vat_check_digits_ok(vat):
        return None
    return _reference("vat_register").get(re.sub(r"\D", "", vat))


def make_vat_number(first_seven: str) -> str:
    """Build a VAT number with valid check digits (for synthetic data)."""
    total = sum(int(first_seven[i]) * w for i, w in enumerate(range(8, 1, -1)))
    return first_seven + f"{(-total) % 97:02d}"


# ---- synthetic seed data ---------------------------------------------------------

def seed() -> None:
    crm, acc, tax, pay, dms = (system(n) for n in ("crm", "accounting", "tax", "payroll", "dms"))
    hlb_vat = make_vat_number("1234567")

    crm.put("accounts", "HLB001", {
        "account_id": "HLB001", "name": "Harbour Lane Bakery Ltd", "type": "limited_company",
        "company_number": "87654321", "vat_number": hlb_vat, "manager": "casey.brown",
        "services": ["accounts", "vat", "payroll"], "payroll": True, "pay_day": 25,
        "address": {"line1": "2 Harbour Lane", "town": "Porthaven", "postcode": "ZZ99 1HL"},
        "bank": {"account_name": "Harbour Lane Bakery Ltd", "sort_code": "11-11-11", "account_number": "11111111"},
        "email_domain": "harbourlane.example"})
    crm.put("contacts", "CON-HLB001-1", {"contact_id": "CON-HLB001-1", "account_id": "HLB001",
                                        "name": "Hannah Lane", "email": "hannah@harbourlane.example",
                                        "phone": "01632 960001", "role": "Director"})
    crm.put("accounts", "FST001", {
        "account_id": "FST001", "name": "Fern Studio", "type": "sole_trader", "utr": "1234567890",
        "manager": "casey.brown", "services": ["self_assessment"], "payroll": False,
        "address": {"line1": "9 Kiln Yard", "town": "Porthaven", "postcode": "ZZ99 6FS"},
        "email_domain": "fernstudio.example"})
    crm.put("contacts", "CON-FST001-1", {"contact_id": "CON-FST001-1", "account_id": "FST001",
                                        "name": "Rowan Fern", "email": "rowan@fernstudio.example",
                                        "phone": "01632 960002", "role": "Owner"})

    acc.put("contacts", "acc-hlb001", {
        "ContactID": "acc-hlb001", "AccountNumber": "HLB001", "Name": "Harbour Lane Bakery Ltd",
        "Addresses": [{"AddressType": "POBOX", "AddressLine1": "2 Harbour Lane", "City": "Porthaven", "PostalCode": "ZZ99 1HL"}],
        "BankAccountDetails": "11-11-11 11111111", "TaxNumber": hlb_vat})
    acc.put("contacts", "acc-fst001", {
        "ContactID": "acc-fst001", "AccountNumber": "FST001", "Name": "Fern Studio",
        "Addresses": [{"AddressType": "POBOX", "AddressLine1": "9 Kiln Yard", "City": "Porthaven", "PostalCode": "ZZ99 6FS"}]})

    tax.put("clients", "HLB001", {"client_code": "HLB001", "name": "Harbour Lane Bakery Ltd", "entity": "company",
                                  "company_number": "87654321", "address": "2 Harbour Lane, Porthaven", "postcode": "ZZ99 1HL"})
    tax.put("clients", "FST001", {"client_code": "FST001", "name": "Fern Studio", "entity": "individual",
                                  "utr": "1234567890", "address": "9 Kiln Yard, Porthaven", "postcode": "ZZ99 6FS"})

    pay.put("employers", "HLB001", {"employer_ref": "HLB001", "name": "Harbour Lane Bakery Ltd",
                                    "address_lines": ["2 Harbour Lane", "Porthaven"], "postcode": "ZZ99 1HL", "pay_day": 25})
    pay.put("employees", "HLB001-E001", {"employee_id": "HLB001-E001", "employer_ref": "HLB001", "forename": "Jamie",
                                         "surname": "Sample", "ni_number": "QQ654321A", "date_of_birth": "1990-05-01",
                                         "start_date": "2024-04-01", "annual_salary": 24500, "status": "active"})

    dms.put("folders", "HLB001", {"client_id": "HLB001", "path": "Clients/HLB001 Harbour Lane Bakery Ltd"})
    dms.put("folders", "FST001", {"client_id": "FST001", "path": "Clients/FST001 Fern Studio"})

    ref_dir = config.sub("reference")
    (ref_dir / "companies_house.json").write_text(json.dumps({
        "87654321": {"name": "HARBOUR LANE BAKERY LTD", "status": "active", "registered_postcode": "ZZ99 1HL",
                     "officers": ["Hannah Lane"]},
        "12345678": {"name": "RIVERSIDE JOINERY LTD", "status": "active", "registered_postcode": "ZZ99 2RJ",
                     "officers": ["Ellis Carter"]},
    }, indent=2), encoding="utf-8")
    (ref_dir / "vat_register.json").write_text(json.dumps({hlb_vat: "Harbour Lane Bakery Ltd"}, indent=2),
                                               encoding="utf-8")
