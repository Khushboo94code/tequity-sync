"""Tests for portal authentication and session-derived attribution.

Covers signup/login/me, duplicate signup, bad credentials, that protected endpoints require a token,
that a portal edit is attributed to the authenticated session user (not anything the client sends),
and the records list.
"""


def signup(client, email, password="pw"):
    return client.post("/auth/signup", json={"email": email, "password": password})


def headersFor(client, email, password="pw"):
    return {"Authorization": f"Bearer {signup(client, email, password).json()['token']}"}


def webhook(client, eventId, recordId, fields):
    """Create a record via the (open) webhook endpoint."""
    return client.post("/webhooks/salesforce", json={
        "eventId": eventId, "recordId": recordId, "objectType": "Contact",
        "occurredAt": "2026-11-02T11:00:00.000Z", "changedBy": "sales.user@client.example",
        "changedFields": fields,
    })


def test_signupLoginAndMe(client):
    s = signup(client, "finance@client.example")
    assert s.status_code == 200 and s.json()["email"] == "finance@client.example"
    login = client.post("/auth/login", json={"email": "finance@client.example", "password": "pw"})
    assert login.status_code == 200
    token = login.json()["token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["email"] == "finance@client.example"


def test_duplicateSignupConflicts(client):
    assert signup(client, "dup@client.example").status_code == 200
    assert signup(client, "dup@client.example").status_code == 409


def test_loginBadCredentials(client):
    signup(client, "a@client.example", "right")
    assert client.post("/auth/login", json={"email": "a@client.example", "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"email": "nobody@client.example", "password": "x"}).status_code == 401


def test_protectedEndpointsRequireToken(client):
    assert client.get("/records").status_code == 401
    assert client.get("/records/0031").status_code == 401
    assert client.put("/portal/records/0031", json={"changedFields": {"city": "Noida"}}).status_code == 401
    # a garbage token is rejected too
    assert client.get("/records", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_editIsAttributedToSessionUser(client):
    webhook(client, "e1", "0031", {"phone": "+91SF"})
    headers = headersFor(client, "finance@client.example")
    res = client.put("/portal/records/0031", json={"changedFields": {"city": "Noida"}}, headers=headers)
    assert res.status_code == 200
    hist = client.get("/records/0031/history", headers=headers).json()["history"]
    cityRow = next(h for h in hist if h["fieldName"] == "city")
    assert cityRow["actor"] == "finance@client.example"   # the session user, not a client-sent value
    assert cityRow["source"] == "portal"


def test_differentUsersAttributedSeparately(client):
    webhook(client, "e1", "0031", {"phone": "+91SF"})
    alice = headersFor(client, "alice@client.example")
    bob = headersFor(client, "bob@client.example")
    client.put("/portal/records/0031", json={"changedFields": {"city": "Noida"}}, headers=alice)
    client.put("/portal/records/0031", json={"changedFields": {"email": "x@y.com"}}, headers=bob)
    hist = client.get("/records/0031/history", headers=alice).json()["history"]
    actors = {h["fieldName"]: h["actor"] for h in hist if h["source"] == "portal"}
    assert actors["city"] == "alice@client.example"
    assert actors["email"] == "bob@client.example"


def test_recordsListAfterCreate(client):
    webhook(client, "e1", "0031", {"fullName": "Aarti", "phone": "+91"})
    webhook(client, "e2", "0032", {"phone": "+92"})
    headers = headersFor(client, "finance@client.example")
    records = client.get("/records", headers=headers).json()["records"]
    byId = {r["recordId"]: r for r in records}
    assert byId["0031"]["displayName"] == "Aarti"   # uses fullName
    assert byId["0032"]["displayName"] == "0032"     # falls back to recordId
