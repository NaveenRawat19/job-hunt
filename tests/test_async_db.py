from jobhunt.api.db import Database, _params


def test_positional_sql_parameters_are_converted_to_named_binds():
    sql, values = _params("SELECT * FROM users WHERE email=%s AND id=%s", ("a@b.com", 7))
    assert sql == "SELECT * FROM users WHERE email=:p0 AND id=:p1"
    assert values == {"p0": "a@b.com", "p1": 7}


def test_named_parameters_are_left_unchanged():
    query = "SELECT * FROM jobs WHERE user_id=:uid"
    params = {"uid": 3}
    assert _params(query, params) == (query, params)


def test_job_filter_builder_uses_bound_parameters():
    clause, values = Database._filters(9, 7.5, "Acme", "python", "India", "new")
    assert "user_id=:u" in clause
    assert "LOWER(company)=LOWER(:company)" in clause
    assert values == {
        "u": 9, "score": 7.5, "company": "Acme", "q": "%python%",
        "loc": "%india%", "status": "new",
    }
