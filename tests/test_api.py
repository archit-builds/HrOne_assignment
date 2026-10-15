"""
Integration tests. They need a running MongoDB and use their own database (attendance_test),
which is emptied before every test.   Run:  pytest -q
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

os.environ["MONGO_DB"] = "attendance_test"  # must be set before app.main is imported

import pytest
from fastapi.testclient import TestClient

from app.main import app, db

IST = timezone(timedelta(hours=5, minutes=30))


def ist(day: str, hhmm: str, seconds: int = 0, millis: int = 0) -> int:
    """Epoch ms for an IST wall-clock time on a 'YYYY-MM-DD' day."""
    dt = datetime.fromisoformat(f"{day}T{hhmm}:{seconds:02d}").replace(tzinfo=IST)
    return int(dt.timestamp()) * 1000 + millis


@pytest.fixture(scope="module")
def api():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db(api):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def make_emp(api, code="EMP0001", dept="Eng", start="09:30", end="18:30", joined="2026-01-01"):
    r = api.post("/employees", json={"emp_code": code, "name": code, "email": "a@b.co", "department": dept,
                                     "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text


def punch_in(api, code, day, hhmm, **extra):
    return api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ist(day, hhmm), **extra})


def seed_log(code, day, status="PRESENT", late=0, overtime=0, hours=None, half=False, punch_in_ms=None):
    """Insert a stored record directly (like the grader's seeding)."""
    pin = None if punch_in_ms is None else datetime.fromtimestamp(punch_in_ms / 1000, timezone.utc)
    db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, "punch_in": pin, "punch_out": None,
                                   "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
                                   "half_day": half, "history": []})


# ----------------------------- employees ----------------------------------- #
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_employee_validation(api):
    base = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-01"}
    assert api.post("/employees", json=base).status_code == 201
    assert api.post("/employees", json=base).status_code == 409
    for bad in ({"emp_code": "EMP12"}, {"emp_code": "X0001"}, {"email": "nope"}, {"joined_on": "2026-02-30"},
                {"shift_start": "9:30"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"name": ""}):
        assert api.post("/employees", json={**base, "emp_code": "EMP0002", **bad}).status_code == 422, bad


def test_employee_created_at_is_epoch_ms_and_listing(api):
    make_emp(api, "EMP0002", "Eng")
    make_emp(api, "EMP0001", "Eng")
    make_emp(api, "EMP0003", "Ops")
    body = api.get("/employees", params={"department": "Eng"}).json()
    assert body["total"] == 2 and [e["emp_code"] for e in body["items"]] == ["EMP0001", "EMP0002"]
    assert isinstance(body["items"][0]["created_at"], int) and "_id" not in body["items"][0]
    page2 = api.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in page2["items"]] == ["EMP0003"] and page2["total"] == 3
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_concurrent_employee_create(api):
    body = {"emp_code": "EMP0009", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: api.post("/employees", json=body).status_code, range(8)))
    assert codes == [201] + [409] * 7


# ----------------------------- punch-in rules ------------------------------ #
@pytest.mark.parametrize("hhmm,sec,expected", [("09:40", 0, 0), ("09:40", 1, 10), ("10:15", 59, 45), ("09:00", 0, 0)])
def test_late_rule(api, hhmm, sec, expected):
    make_emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-06", hhmm, sec)})
    assert r.status_code == 201 and r.json()["late_minutes"] == expected
    assert "id" not in r.json() and r.json()["history"] == []


def test_punch_in_truncates_milliseconds(api):
    make_emp(api)
    ms = ist("2026-07-06", "09:40", 0, 900)  # 09:40:00.900 counts as 09:40:00 -> not late
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
    assert r.json()["late_minutes"] == 0 and r.json()["punch_in"] == ms - 900


def test_punch_in_errors(api):
    make_emp(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    for bad in (0, 1783312500, 1783312500000.5, "1783312500000", 5_000_000_000_000, True):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert punch_in(api, "EMP0001", "2026-07-06", "09:30").status_code == 201
    assert punch_in(api, "EMP0001", "2026-07-06", "09:31").status_code == 409


def test_derived_fields_from_client_are_ignored(api):
    make_emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-06", "09:30"),
                                               "late_minutes": 99, "work_hours": 7})
    assert r.json()["late_minutes"] == 0 and r.json()["work_hours"] is None


def test_overnight_attendance_date(api):
    make_emp(api, start="22:00", end="06:00")
    assert punch_in(api, "EMP0001", "2026-07-07", "05:30").json()["date"] == "2026-07-06"  # before shift_end
    db.attendance_logs.delete_many({})
    assert punch_in(api, "EMP0001", "2026-07-06", "21:55").json()["date"] == "2026-07-06"
    db.attendance_logs.delete_many({})
    assert punch_in(api, "EMP0001", "2026-07-07", "06:00").json()["date"] == "2026-07-07"  # not earlier than end


def test_concurrent_punch_in(api):
    make_emp(api)
    payload = {"emp_code": "EMP0001", "punched_at": ist("2026-07-06", "09:30")}
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: api.post("/attendance/punch-in", json=payload).status_code, range(8)))
    assert codes == [201] + [409] * 7


# ----------------------------- punch-out rules ----------------------------- #
def punch_out(api, day, hhmm, code="EMP0001", **kw):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ist(day, hhmm, **kw)})


@pytest.mark.parametrize("out,overtime", [("18:59", 0), ("19:00", 30)])
def test_overtime_threshold(api, out, overtime):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "09:30")
    r = punch_out(api, "2026-07-06", out)
    assert r.status_code == 200 and r.json()["overtime_minutes"] == overtime


def test_work_hours_and_half_day(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "09:30")
    r = punch_out(api, "2026-07-06", "14:00")  # exactly 4.50 -> not a half day
    assert r.json()["work_hours"] == 4.5 and r.json()["half_day"] is False
    make_emp(api, "EMP0002")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ist("2026-07-06", "09:30")})
    r = punch_out(api, "2026-07-06", "13:59", code="EMP0002")
    assert r.json()["work_hours"] == 4.48 and r.json()["half_day"] is True


def test_work_hours_rounds_half_up(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "09:30")
    # 4h 0m 18s = 4.005h -> 4.01 (half-up); round() would give 4.0
    r = punch_out(api, "2026-07-06", "13:30", seconds=18)
    assert r.json()["work_hours"] == 4.01


def test_overnight_punch_out(api):
    make_emp(api, start="22:00", end="06:00")
    punch_in(api, "EMP0001", "2026-07-06", "21:55")
    r = punch_out(api, "2026-07-07", "06:40")
    body = r.json()
    assert r.status_code == 200 and body["date"] == "2026-07-06"
    assert body["overtime_minutes"] == 40 and body["work_hours"] == 8.75


def test_punch_out_errors(api):
    make_emp(api)
    assert punch_out(api, "2026-07-06", "18:00", code="EMP9999").status_code == 404
    assert punch_out(api, "2026-07-06", "18:00").status_code == 404  # no punch-in at all
    punch_in(api, "EMP0001", "2026-07-06", "09:30")
    assert punch_out(api, "2026-07-06", "09:00").status_code == 404  # punch-in is after punched_at
    assert punch_out(api, "2026-07-06", "09:30").status_code == 422  # not after punch-in
    assert punch_out(api, "2026-07-07", "09:31").status_code == 422  # more than 24h
    assert punch_out(api, "2026-07-06", "18:00").status_code == 200
    assert punch_out(api, "2026-07-06", "18:05").status_code == 409


def test_concurrent_punch_out(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "09:30")
    with ThreadPoolExecutor(8) as pool:
        codes = sorted(pool.map(lambda _: punch_out(api, "2026-07-06", "18:00").status_code, range(8)))
    assert codes == [200] + [409] * 7


# ----------------------------- list attendance ----------------------------- #
def test_attendance_list(api):
    for code in ("EMP0002", "EMP0001"):
        make_emp(api, code)
        for day in ("2026-07-06", "2026-07-07"):
            seed_log(code, day, punch_in_ms=ist(day, "09:30"))
    seed_log("EMP0001", "2026-07-08", status="LEAVE")
    body = api.get("/attendance").json()
    keys = [(r["date"], r["emp_code"]) for r in body["items"]]
    assert keys == [("2026-07-08", "EMP0001"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"),
                    ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert body["total"] == 5 and "id" not in body["items"][0]
    assert isinstance(body["items"][1]["punch_in"], int)
    f = api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07", "page_size": 1, "page": 2}).json()
    assert f["total"] == 2 and [r["emp_code"] for r in f["items"]] == ["EMP0002"]
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    assert api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422


def test_legacy_record_defaults(api):
    db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                   "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0})
    rec = api.get("/attendance").json()["items"][0]
    assert rec["history"] == [] and rec["half_day"] is False


# ----------------------------- PATCH ---------------------------------------- #
def patch(api, day, body, code="EMP0001"):
    return api.patch(f"/attendance/{code}/{day}", json={"reason": "fixing it", "regularized_by": "hr", **body})


def test_patch_recomputes_and_records_history(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "10:05")
    punch_out(api, "2026-07-06", "19:10")
    r = patch(api, "2026-07-06", {"punch_in": ist("2026-07-06", "09:28")})
    body = r.json()
    assert r.status_code == 200 and body["late_minutes"] == 0 and body["work_hours"] == 9.7
    assert len(body["history"]) == 1
    changes = body["history"][0]["changes"]
    assert set(changes) == {"punch_in", "late_minutes", "work_hours"}  # overtime/half_day did not change
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["punch_in"] == {"from": ist("2026-07-06", "10:05"), "to": ist("2026-07-06", "09:28")}
    assert isinstance(body["history"][0]["at"], int)
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == 1 and isinstance(stored["history"][0]["changes"]["punch_in"]["to"], datetime)
    assert patch(api, "2026-07-06", {"status": "WFH"}).json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_patch_rules(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "10:05")
    day = "2026-07-06"
    assert patch(api, day, {}).status_code == 422  # nothing changes
    assert patch(api, day, {"status": "PRESENT"}).status_code == 422  # same value = no-op
    assert patch(api, day, {"status": "LEAVE", "punch_in": ist(day, "09:00")}).status_code == 422
    assert patch(api, day, {"punch_in": ist("2026-07-07", "09:00")}).status_code == 422  # leaves the attendance date
    assert patch(api, day, {"punch_out": ist(day, "10:05")}).status_code == 422  # not after punch-in
    assert patch(api, day, {"punch_out": ist("2026-07-07", "10:06")}).status_code == 422  # > 24h
    assert patch(api, "2026-07-09", {"status": "LEAVE"}).status_code == 404
    assert patch(api, day, {"status": "LEAVE"}, code="EMP9999").status_code == 404
    assert api.patch(f"/attendance/EMP0001/{day}", json={"status": "LEAVE"}).status_code == 422  # reason required
    r = patch(api, day, {"status": "LEAVE"})  # clears punch times
    assert r.status_code == 200 and r.json()["punch_in"] is None and r.json()["late_minutes"] == 0
    assert patch(api, day, {"status": "PRESENT"}).status_code == 422  # presence needs a punch_in
    assert patch(api, day, {"status": "WFH", "punch_in": ist(day, "09:00")}).status_code == 200


def test_concurrent_patch_never_loses_history(api):
    make_emp(api)
    punch_in(api, "EMP0001", "2026-07-06", "10:05")

    def go(i):
        return patch(api, "2026-07-06", {"punch_in": ist("2026-07-06", f"09:{10 + i}")}).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(go, range(6)))
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert set(codes) <= {200, 409, 422}
    assert len(stored["history"]) == codes.count(200)  # every success is in the audit trail


# ----------------------------- analytics ------------------------------------ #
def test_employee_monthly(api):
    make_emp(api, joined="2026-07-15")  # mid-month joiner: weekdays 15-31 July = 13
    # Wed 15 (full), Thu 16 (half day, late), Sat 18 (weekend: present not counted, late counted), Fri 17 leave
    seed_log("EMP0001", "2026-07-15", punch_in_ms=ist("2026-07-15", "09:30"), hours=9, overtime=40)
    seed_log("EMP0001", "2026-07-16", punch_in_ms=ist("2026-07-16", "10:00"), hours=3, late=30, half=True)
    seed_log("EMP0001", "2026-07-17", status="LEAVE")
    seed_log("EMP0001", "2026-07-18", punch_in_ms=ist("2026-07-18", "10:00"), hours=8, late=30, overtime=35)
    r = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert r.json() == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 13, "present_days": 1.5,
                        "leave_days": 1, "late_count": 2, "total_late_minutes": 60, "total_overtime_minutes": 75,
                        "attendance_pct": 11.54}
    assert api.get("/analytics/employees/EMP0009/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    # joined after the month -> no working days, null pct; no logs -> zeros
    r = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"}).json()
    assert r["working_days"] == 0 and r["attendance_pct"] is None and r["present_days"] == 0


def test_department_summary(api):
    make_emp(api, "EMP0001", "Eng")
    make_emp(api, "EMP0002", "Eng")
    make_emp(api, "EMP0003", "Eng", joined="2026-08-01")  # joins later: excluded
    make_emp(api, "EMP0004", "Ops")  # no logs at all: counted
    make_emp(api, "EMP0005", "Late", joined="2026-09-01")  # department with zero headcount: omitted
    seed_log("EMP0001", "2026-07-06", punch_in_ms=ist("2026-07-06", "09:30"), hours=9.0, late=15)
    seed_log("EMP0001", "2026-07-07", punch_in_ms=ist("2026-07-07", "09:30"), hours=3.0, half=True)
    seed_log("EMP0002", "2026-07-06", status="ON_DUTY", punch_in_ms=ist("2026-07-06", "09:30"), hours=6.0)
    seed_log("EMP0002", "2026-07-07", status="LEAVE")
    seed_log("EMP0002", "2026-07-08", status="ABSENT")
    seed_log("EMP0002", "2026-07-09", punch_in_ms=ist("2026-07-09", "09:30"))  # open record: no hours
    body = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body["month"] == "2026-07"
    assert body["items"] == [
        {"department": "Eng", "headcount": 2, "present_days": 3.5, "avg_work_hours": 6.0, "late_count": 1,
         "total_late_minutes": 15, "leave_count": 1, "on_duty_count": 1},
        {"department": "Ops", "headcount": 1, "present_days": 0, "avg_work_hours": None, "late_count": 0,
         "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0},
    ]
    only = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert [i["department"] for i in only["items"]] == ["Ops"]


def test_late_leaderboard_ties_and_limit(api):
    for i, (code, dept) in enumerate([("EMP0001", "Eng"), ("EMP0002", "Eng"), ("EMP0003", "Ops"),
                                      ("EMP0004", "Ops"), ("EMP0005", "Ops")], start=1):
        make_emp(api, code, dept)
    seed_log("EMP0001", "2026-07-06", late=50)
    seed_log("EMP0002", "2026-07-06", late=30)
    seed_log("EMP0002", "2026-07-07", late=10)  # EMP0002 total 40
    seed_log("EMP0003", "2026-07-06", late=40)  # ties with EMP0002 at 40
    seed_log("EMP0004", "2026-07-06", late=20)
    seed_log("EMP0005", "2026-07-06", late=0)  # total 0: omitted
    seed_log("EMP0777", "2026-07-06", late=99)  # unknown employee: ignored
    seed_log("EMP0001", "2026-08-01", late=500)  # other month
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [
        (1, "EMP0001", 50), (2, "EMP0002", 40), (2, "EMP0003", 40), (4, "EMP0004", 20)]
    assert rows[1]["late_count"] == 2 and rows[2]["department"] == "Ops" and rows[0]["name"] == "EMP0001"
    # limit=2 keeps both rank-2 rows (3 rows), limit=3 also (rank 4 > 3)
    assert len(api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]) == 3
    assert len(api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"]) == 1
    ops = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Ops"}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in ops] == [(1, "EMP0003"), (2, "EMP0004")]
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend(api):
    make_emp(api, "EMP0001", "Eng", joined="2026-07-01")
    make_emp(api, "EMP0002", "Eng", joined="2026-07-08")
    # Mon 6, Tue 7, Wed 8 ... Sun 12, Mon 13
    seed_log("EMP0001", "2026-07-06", punch_in_ms=ist("2026-07-06", "09:30"), late=20)
    seed_log("EMP0001", "2026-07-08", punch_in_ms=ist("2026-07-08", "09:30"))
    seed_log("EMP0002", "2026-07-08", punch_in_ms=ist("2026-07-08", "09:30"), half=True)
    seed_log("EMP0001", "2026-07-11", punch_in_ms=ist("2026-07-11", "09:30"))  # Saturday record
    seed_log("EMP0002", "2026-07-09", status="LEAVE")
    r = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06", "to": "2026-07-13"})
    rows = r.json()["items"]
    assert r.json()["department"] == "Eng" and [x["date"] for x in rows] == [f"2026-07-{d:02d}" for d in range(6, 14)]
    by = {x["date"]: x for x in rows}
    assert by["2026-07-06"] == {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 1,
                                "late_count": 1, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert by["2026-07-07"]["attendance_rate"] == 0.0 and by["2026-07-07"]["moving_avg_7d"] == 0.5  # no records
    assert by["2026-07-08"]["headcount"] == 2 and by["2026-07-08"]["present_count"] == 1.5
    assert by["2026-07-08"]["attendance_rate"] == 0.75
    assert by["2026-07-09"]["present_count"] == 0 and by["2026-07-09"]["attendance_rate"] == 0.0
    sat = by["2026-07-11"]
    assert sat["is_working_day"] is False and sat["present_count"] == 1 and sat["attendance_rate"] is None
    assert by["2026-07-12"]["attendance_rate"] is None and by["2026-07-12"]["present_count"] == 0
    # Mon 6..Fri 10 rates: 1, 0, .75, 0, 0 ; avg over rows 6..12 ignores the weekend nulls
    assert by["2026-07-12"]["moving_avg_7d"] == 0.35  # (1+0+.75+0+0)/5
    assert by["2026-07-13"]["moving_avg_7d"] == 0.15  # window Jul 7..13: 0, .75, 0, 0, 0
    # all-weekend window -> null average
    wk = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-11", "to": "2026-07-12"}).json()["items"]
    assert [x["moving_avg_7d"] for x in wk] == [None, None]


def test_department_trend_everyone_joins_later(api):
    make_emp(api, "EMP0001", "Eng", joined="2026-09-01")
    rows = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06", "to": "2026-07-08"}).json()["items"]
    assert len(rows) == 3 and all(r["headcount"] == 0 and r["attendance_rate"] is None for r in rows)
    assert all(r["moving_avg_7d"] is None for r in rows)


def test_department_trend_errors(api):
    make_emp(api, "EMP0001", "Eng")
    p = {"from": "2026-07-06", "to": "2026-07-13"}
    assert api.get("/analytics/departments/Nope/trend", params=p).status_code == 404
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-13", "to": "2026-07-06"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-06-30"}).status_code == 422
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06"}).status_code == 422
    one = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"]
    assert len(one) == 1


# ----------------------------- explain -------------------------------------- #
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2026-07-01"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_endpoints_run(api, endpoint, params):
    make_emp(api)
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    assert r.json()["endpoint"] == endpoint and "explain" in r.json()


def test_explain_missing_params(api):
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert api.get("/admin/explain/late_leaderboard").status_code == 422
    assert api.get("/admin/explain/unknown").status_code == 422
