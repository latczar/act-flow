import datetime as dt

from act_flow import systems
from act_flow.extraction import parse_date
from act_flow.validation import grounded, normalise, same_person


def test_national_insurance_numbers(env, monkeypatch):
    assert normalise("ni", "AB 12 34 56 C") == ("AB123456C", None)
    assert normalise("ni", "DA123456A")[1]                 # D is never a valid first letter
    assert normalise("ni", "QQ 12 34 56 C") == ("QQ123456C", None)   # HMRC's example prefix, test mode only
    monkeypatch.setenv("ACT_ALLOW_TEST_IDS", "0")
    assert normalise("ni", "QQ123456C")[1]


def test_formats():
    assert normalise("postcode", "zz99 4qs") == ("ZZ99 4QS", None)
    assert normalise("postcode", "12345")[1]
    assert normalise("sort_code", "123456") == ("12-34-56", None)
    assert normalise("sort_code", "12-34-5")[1]
    assert normalise("account_number", "1234 5678") == ("12345678", None)
    assert normalise("money", "£28,500") == ("28500", None)
    assert normalise("money", "-5")[1]
    assert normalise("company_number", "sc123456") == ("SC123456", None)
    assert normalise("statement", "d")[1]
    assert normalise("choice", "tax return", "purpose") == ("tax_return", None)
    assert normalise("text", "")[1] == "missing"


def test_dates(env):
    assert parse_date("1st December 2026") == dt.date(2026, 12, 1)
    assert parse_date("01/12/2026") == dt.date(2026, 12, 1)          # UK order
    assert parse_date("2 December") == dt.date(2026, 12, 2)           # year inferred from 23 Nov 2026
    assert parse_date("5 January") == dt.date(2027, 1, 5)
    assert parse_date("31/02/2026") is None
    assert normalise("date", "not a date")[1]


def test_vat_check_digits():
    vat = systems.make_vat_number("1234567")
    assert systems.vat_check_digits_ok(vat)
    assert systems.vat_check_digits_ok("GB" + vat)
    assert not systems.vat_check_digits_ok(vat[:-1] + str((int(vat[-1]) + 1) % 10))


def test_grounding_catches_invented_values():
    text = "Please add Sam Example to payroll on £28,500 a year."
    assert grounded("money", "28500", "£28,500 a year", text) is None
    assert grounded("money", "29500", "£28,500 a year", text)                 # value does not match quote
    assert grounded("money", "28500", "£28,500 per annum", text)              # quote not in source
    assert grounded("text", "Sam Example", None, text)                        # no quote at all
    assert grounded("date", "2026-12-02", "from 2 December", "starting from 2 December") is None


def test_same_person():
    assert same_person("Ellis James Carter", "Ellis Carter")
    assert not same_person("Ellis Carter", "Ellis Parker")
