"""Customer add / inquire / update (LGACUS01, LGICUS01, LGUCUS01)."""


def test_add_customer_stores_db_row_and_vsam_record(genapp, db, new_customer):
    num = new_customer(first_name="Grace", last_name="Hopper")

    row = db.execute(
        "SELECT firstname, lastname, dateofbirth::text, postcode, emailaddress "
        "FROM customer WHERE customernumber = %s", (num,)).fetchone()
    assert row is not None
    assert [v.rstrip() for v in row] == ["Grace", "Hopper", "1815-12-10", "SO211AB", "ada@example.com"]

    secure = db.execute("SELECT state_indicator, pass_changes FROM customer_secure "
                        "WHERE customernumber = %s", (num,)).fetchone()
    assert secure is not None

    record = genapp.vsam("KSDSCUST", f"{num:010d}")
    assert record is not None, "customer record missing from KSDSCUST"
    assert b"Grace" in record


def test_inquire_customer(genapp, customer):
    r = genapp.call("LGICUS01", genapp.commarea(request_id="01ICUS", customer_num=customer))
    assert r.abend is None, r.log
    assert r.return_code == "00"
    assert r.ca["CA-FIRST-NAME"] == "Ada"
    assert r.ca["CA-LAST-NAME"] == "Lovelace"
    assert r.ca["CA-DOB"] == "1815-12-10"
    assert r.ca["CA-POSTCODE"] == "SO211AB"


def test_inquire_unknown_customer_returns_01(genapp):
    r = genapp.call("LGICUS01", genapp.commarea(request_id="01ICUS", customer_num=9999999))
    assert r.return_code == "01"


def test_update_customer(genapp, db, customer):
    r = genapp.call("LGICUS01", genapp.commarea(request_id="01ICUS", customer_num=customer))
    ca = r.ca
    ca["CA-REQUEST-ID"] = "01UCUS"
    ca["CA-LAST-NAME"] = "King"
    ca["CA-EMAIL-ADDRESS"] = "ada.king@example.com"

    r = genapp.call("LGUCUS01", ca)
    assert r.abend is None, r.log
    assert r.return_code == "00"

    row = db.execute("SELECT lastname, emailaddress FROM customer WHERE customernumber = %s",
                     (customer,)).fetchone()
    assert [v.rstrip() for v in row] == ["King", "ada.king@example.com"]
    assert b"King" in genapp.vsam("KSDSCUST", f"{customer:010d}")


def test_update_customer_wrong_request_id_returns_99(genapp, customer):
    r = genapp.call("LGUCUS01", genapp.commarea(request_id="01XCUS", customer_num=customer))
    assert r.return_code == "99"


def test_short_commarea_returns_98(genapp):
    ca = genapp.commarea(request_id="01ICUS", customer_num=1)
    r = genapp.call("LGICUS01", ca, calen=20)
    assert r.return_code == "98"


def test_no_commarea_abends_lgca(genapp):
    r = genapp.call("LGICUS01", b"", calen=0)
    assert r.abend == "LGCA"
