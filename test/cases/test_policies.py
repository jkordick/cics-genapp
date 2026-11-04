"""Policy add / inquire / update / delete through the business programs
LGAPOL01, LGIPOL01, LGUPOL01 and LGDPOL01 (which LINK to the Db2 and VSAM
programs), for each policy type.

To cover a new policy type, add an entry to POLICY_TYPES. Every test below
that is parametrized over POLICY_TYPES then runs for it too. Set update={}
and delete=False while the type does not support update / delete yet.
"""
from dataclasses import dataclass
from datetime import date

import pytest

COMMON = dict(issue_date="2026-01-01", expiry_date="2027-01-01", brokerid=0,
              brokersref="", payment=0)


@dataclass
class PolicyType:
    code: str            # request id suffix, e.g. 'MOT' -> 01AMOT / 01IMOT ...
    vsam_type: str       # first byte of the KSDSPOLY key
    table: str           # Db2 table holding the type-specific columns
    fields: dict         # COMMAREA fields for add
    db_columns: dict     # table column -> expected value after add
    update: dict         # COMMAREA fields to change on update (empty = not supported)
    updated_columns: dict  # table column -> expected value after update
    delete: bool = True  # False = 01D<code> not supported

    def __str__(self):
        return self.code


POLICY_TYPES = [
    PolicyType(
        "MOT", "M", "motor",
        fields=dict(m_make="Ford", m_model="Focus", m_value=15000, m_regnumber="AB12CDE",
                    m_colour="Blue", m_cc=1600, m_manufactured="2020-05-01",
                    m_premium=500, m_accidents=0),
        db_columns=dict(make="Ford", model="Focus", value=15000, regnumber="AB12CDE",
                        cc=1600, premium=500),
        update=dict(m_colour="Red", m_premium=650, m_accidents=1),
        updated_columns=dict(colour="Red", premium=650, accidents=1),
    ),
    PolicyType(
        "HOU", "H", "house",
        fields=dict(h_property_type="Detached", h_bedrooms=3, h_value=250000,
                    h_house_name="Rose Cottage", h_house_number="12", h_postcode="SO211AB"),
        db_columns=dict(propertytype="Detached", bedrooms=3, value=250000,
                        housename="Rose Cottage", postcode="SO211AB"),
        update=dict(h_bedrooms=4, h_value=300000),
        updated_columns=dict(bedrooms=4, value=300000),
    ),
    PolicyType(
        "END", "E", "endowment",
        fields=dict(e_with_profits="Y", e_equities="N", e_managed_fund="Y",
                    e_fund_name="GROWTH", e_term=20, e_sum_assured=100000,
                    e_life_assured="Ada Lovelace"),
        db_columns=dict(withprofits="Y", equities="N", managedfund="Y", fundname="GROWTH",
                        term=20, sumassured=100000, lifeassured="Ada Lovelace"),
        update=dict(e_term=25, e_sum_assured=120000),
        updated_columns=dict(term=25, sumassured=120000),
    ),
    PolicyType(
        "COM", "C", "commercial",
        fields=dict(b_address="1 High Street", b_postcode="SO211AB", b_latitude="51.0632N",
                    b_longitude="1.3080W", b_customer="Ada Ltd", b_proptype="Office",
                    b_fireperil=1, b_firepremium=100, b_crimeperil=2, b_crimepremium=200,
                    b_floodperil=3, b_floodpremium=300, b_weatherperil=4,
                    b_weatherpremium=400, b_status=0, b_rejectreason=""),
        db_columns=dict(address="1 High Street", zipcode="SO211AB", customer="Ada Ltd",
                        firepremium=100, weatherpremium=400),
        update={},           # GenApp has no update for commercial policies
        updated_columns={},
    ),
    PolicyType(
        "PET", "P", "pet",
        fields=dict(pt_name="Milo", pt_species="DOG", pt_breed="Border Collie",
                    pt_dob="2021-06-15", pt_microchip="985112004123456",
                    pt_value=1200, pt_vet_limit=5000, pt_premium=300),
        db_columns=dict(petname="Milo", species="DOG", breed="Border Collie",
                        dateofbirth=date(2021, 6, 15), microchip="985112004123456",
                        value=1200, vetfeelimit=5000, premium=300),
        update={},
        updated_columns={},
        delete=False,
    ),
]


def add_policy(genapp, customer, ptype, **overrides):
    ca = genapp.commarea(request_id=f"01A{ptype.code}", customer_num=customer, policy_num=0,
                         **{**COMMON, **ptype.fields, **overrides})
    return genapp.call("LGAPOL01", ca)


def inquire_policy(genapp, customer, ptype, policy):
    return genapp.call("LGIPOL01", genapp.commarea(
        request_id=f"01I{ptype.code}", customer_num=customer, policy_num=policy))


def db_row(db, table, policy, columns):
    cols = ", ".join(columns)
    row = db.execute(f"SELECT {cols} FROM {table} WHERE policynumber = %s", (policy,)).fetchone()
    if row is None:
        return None
    return {c: (v.rstrip() if isinstance(v, str) else v) for c, v in zip(columns, row)}


@pytest.mark.parametrize("ptype", POLICY_TYPES, ids=str)
def test_add_policy(genapp, db, customer, ptype):
    r = add_policy(genapp, customer, ptype)
    assert r.abend is None, r.log
    assert r.return_code == "00"
    policy = r.ca["CA-POLICY-NUM"]
    assert isinstance(policy, int) and policy > 0

    common = db.execute("SELECT customernumber, policytype, issuedate::text, expirydate::text "
                        "FROM policy WHERE policynumber = %s", (policy,)).fetchone()
    assert common == (customer, ptype.vsam_type, "2026-01-01", "2027-01-01")
    assert db_row(db, ptype.table, policy, list(ptype.db_columns)) == ptype.db_columns

    key = f"{ptype.vsam_type}{customer:010d}{policy:010d}"
    assert genapp.vsam("KSDSPOLY", key) is not None, "policy record missing from KSDSPOLY"


@pytest.mark.parametrize("ptype", POLICY_TYPES, ids=str)
def test_inquire_policy_returns_added_values(genapp, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]

    r = inquire_policy(genapp, customer, ptype, policy)
    assert r.abend is None, r.log
    assert r.return_code == "00"
    assert r.ca["CA-ISSUE-DATE"] == "2026-01-01"
    assert r.ca["CA-EXPIRY-DATE"] == "2027-01-01"
    assert len(r.ca["CA-LASTCHANGED"]) == 26      # Db2 timestamp
    for name, value in ptype.fields.items():
        assert r.ca[name] == value, name


@pytest.mark.parametrize("ptype", POLICY_TYPES, ids=str)
def test_inquire_unknown_policy_returns_01(genapp, customer, ptype):
    r = inquire_policy(genapp, customer, ptype, 9999999)
    assert r.return_code == "01"


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_policy(genapp, db, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]
    ca = inquire_policy(genapp, customer, ptype, policy).ca
    before = ca["CA-LASTCHANGED"]

    ca["CA-REQUEST-ID"] = f"01U{ptype.code}"
    for name, value in ptype.update.items():
        ca[name] = value
    r = genapp.call("LGUPOL01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "00"
    assert r.ca["CA-LASTCHANGED"] != before
    assert "FILE REWR KSDSPOLY resp=0" in r.log

    assert db_row(db, ptype.table, policy, list(ptype.updated_columns)) == ptype.updated_columns
    after = inquire_policy(genapp, customer, ptype, policy)
    for name, value in ptype.update.items():
        assert after.ca[name] == value, name


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_with_stale_timestamp_changes_nothing(genapp, db, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]
    ca = inquire_policy(genapp, customer, ptype, policy).ca
    ca["CA-REQUEST-ID"] = f"01U{ptype.code}"
    assert genapp.call("LGUPOL01", ca.copy()).return_code == "00"
    after_first = db_row(db, ptype.table, policy, list(ptype.updated_columns))

    # Second update still carries the old LASTCHANGED: someone else changed
    # the policy in between, so LGUPDB01 must not apply it.
    stale = ca.copy()
    for name, value in ptype.update.items():
        stale[name] = ptype.fields.get(name, value)
    r = genapp.call("LGUPOL01", stale)
    assert db_row(db, ptype.table, policy, list(ptype.updated_columns)) == after_first
    assert r.abend is None, r.log


# Trace line written when LGUPVS01 rewrites the policy's VSAM record. A failed
# update must not reach LGUPVS01, even if the rewrite would not change any
# bytes (Motor and Endowment updates only change columns not kept in VSAM).
VSAM_REWRITE = "FILE REWR KSDSPOLY"


def policy_state(genapp, db, customer, ptype, policy):
    """Everything an update can change: POLICY row, type-specific row, VSAM record."""
    common = db.execute("SELECT * FROM policy WHERE policynumber = %s", (policy,)).fetchone()
    specific = db.execute(f"SELECT * FROM {ptype.table} WHERE policynumber = %s",
                          (policy,)).fetchone()
    key = f"{ptype.vsam_type}{customer:010d}{policy:010d}"
    return common, specific, genapp.vsam("KSDSPOLY", key)


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_with_stale_timestamp_returns_02(genapp, db, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]
    ca = inquire_policy(genapp, customer, ptype, policy).ca
    ca["CA-REQUEST-ID"] = f"01U{ptype.code}"
    first = ca.copy()
    for name, value in ptype.update.items():
        first[name] = value
    assert genapp.call("LGUPOL01", first).return_code == "00"
    before = policy_state(genapp, db, customer, ptype, policy)

    # ca still carries the old LASTCHANGED (and the old values).
    r = genapp.call("LGUPOL01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "02", r.log
    assert policy_state(genapp, db, customer, ptype, policy) == before
    assert VSAM_REWRITE not in r.log


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_unknown_policy_returns_01(genapp, customer, ptype):
    ca = genapp.commarea(request_id=f"01U{ptype.code}", customer_num=customer,
                         policy_num=9999999, **{**COMMON, **ptype.fields})
    r = genapp.call("LGUPOL01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "01", r.log
    assert VSAM_REWRITE not in r.log


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_with_missing_type_specific_row_returns_01(genapp, db, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]
    ca = inquire_policy(genapp, customer, ptype, policy).ca
    # The POLICY row exists, but the UPDATE of the type-specific table finds
    # no row (SQLCODE 100).
    db.execute(f"DELETE FROM {ptype.table} WHERE policynumber = %s", (policy,))
    before = policy_state(genapp, db, customer, ptype, policy)

    ca["CA-REQUEST-ID"] = f"01U{ptype.code}"
    for name, value in ptype.update.items():
        ca[name] = value
    r = genapp.call("LGUPOL01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "01", r.log
    assert policy_state(genapp, db, customer, ptype, policy) == before
    assert VSAM_REWRITE not in r.log


def test_update_motor_with_sql_error_returns_90(genapp, db, customer):
    ptype = next(p for p in POLICY_TYPES if p.code == "MOT")
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]
    ca = inquire_policy(genapp, customer, ptype, policy).ca
    before = policy_state(genapp, db, customer, ptype, policy)
    logged = len(genapp.queue("TD", "CSMT"))

    # Not a valid date, so the UPDATE of the MOTOR table fails (SQLCODE < 0).
    ca["CA-REQUEST-ID"] = "01UMOT"
    ca["CA-M-MANUFACTURED"] = "2020-13-45"
    r = genapp.call("LGUPOL01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "90", r.log
    assert policy_state(genapp, db, customer, ptype, policy) == before
    assert VSAM_REWRITE not in r.log
    assert any("UPDATE MOTOR" in line for line in genapp.queue("TD", "CSMT")[logged:])


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.delete], ids=str)
def test_delete_policy(genapp, db, customer, ptype):
    policy = add_policy(genapp, customer, ptype).ca["CA-POLICY-NUM"]

    r = genapp.call("LGDPOL01", genapp.commarea(
        request_id=f"01D{ptype.code}", customer_num=customer, policy_num=policy))
    assert r.abend is None, r.log
    assert r.return_code == "00"

    assert db.execute("SELECT 1 FROM policy WHERE policynumber = %s", (policy,)).fetchone() is None
    assert db_row(db, ptype.table, policy, list(ptype.db_columns)) is None
    assert genapp.vsam("KSDSPOLY", f"{ptype.vsam_type}{customer:010d}{policy:010d}") is None
    assert inquire_policy(genapp, customer, ptype, policy).return_code == "01"


def test_add_policy_for_unknown_customer_returns_70(genapp):
    r = add_policy(genapp, 9999999, POLICY_TYPES[0])
    assert r.abend is None, r.log
    assert r.return_code == "70"
    assert any("INSERT POLICY" in line for line in genapp.queue("TD", "CSMT"))


@pytest.mark.parametrize("program,request_id", [
    ("LGAPOL01", "01AXXX"), ("LGIPOL01", "01IXXX"), ("LGDPOL01", "01DXXX"),
    # Not LGUPOL01: it sets '99' but still LINKs to LGUPDB01 (GenApp quirk).
])
def test_unknown_request_id_returns_99(genapp, customer, program, request_id):
    r = genapp.call(program, genapp.commarea(request_id=request_id, customer_num=customer, policy_num=1))
    assert r.return_code == "99"


# LGAPOL01 passes the full 32500 bytes on to LGAPDB01, so the per-type
# length check in LGAPDB01 is tested by calling it directly.
@pytest.mark.parametrize("ptype", POLICY_TYPES, ids=str)
def test_add_policy_with_header_only_commarea_returns_98(genapp, customer, ptype):
    ca = genapp.commarea(request_id=f"01A{ptype.code}", customer_num=customer)
    assert genapp.call("LGAPDB01", ca, calen=28).return_code == "98"


@pytest.mark.parametrize("ptype", [p for p in POLICY_TYPES if p.update], ids=str)
def test_update_policy_with_header_only_commarea_returns_98(genapp, customer, ptype):
    ca = genapp.commarea(request_id=f"01U{ptype.code}", customer_num=customer)
    assert genapp.call("LGUPOL01", ca, calen=28).return_code == "98"


# LGAPOL01 and LGDPOL01 themselves only require the 28-byte header.
@pytest.mark.parametrize("program,request_id", [("LGAPOL01", "01AMOT"), ("LGDPOL01", "01DMOT")])
def test_commarea_shorter_than_header_returns_98(genapp, program, request_id):
    r = genapp.call(program, genapp.commarea(request_id=request_id, customer_num=1), calen=20)
    assert r.return_code == "98"
