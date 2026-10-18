import re

PASSWORD = "only-for-tests-password"


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def login(client):
    token = csrf(client.get("/admin/login"))
    response = client.post(
        "/admin/login",
        data={
            "username": "admin",
            "password": PASSWORD,
            "csrf": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf(client.get("/admin"))
