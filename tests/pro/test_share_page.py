from fastapi.testclient import TestClient

from main import app


def test_the_share_page_is_public_and_hands_the_code_to_the_app():
    response = TestClient(app).get("/s")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ebookaipro://share/" in response.text
    assert "package=com.dev.kamalelmagouri.ebook_ai_pro" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
