"""Minimal self-check for ShareCV's non-trivial logic. Run: python tests/test_sharecv.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sharecv import ClipItem, ClipboardBackend, LocalClipboard, hash_text, token_ok
import sharecv


class FakeBackend(ClipboardBackend):
    def __init__(self):
        self.item = ClipItem(type="text", text="", hash=hash_text(""))

    def read(self):
        return self.item

    def write(self, item):
        self.item = item


def test_echo_suppression():
    local = LocalClipboard(FakeBackend())
    local.prime()
    assert local.poll() is None, "primed content must not be reported as a change"

    local.backend.write(ClipItem(type="text", text="hi", hash=hash_text("hi")))
    got = local.poll()
    assert got is not None and got.text == "hi", "real local change must be reported"
    assert local.poll() is None, "same content must not be reported twice"

    incoming = ClipItem(type="file", files=[{"path": "/x/a.txt", "name": "a.txt", "hash": "f" * 64}])
    local.apply(incoming)
    assert local.poll() is None, "applied remote content must not echo back"


def test_wire_roundtrip():
    item = ClipItem(type="file", files=[
        {"path": "/local/a.txt", "name": "a.txt", "hash": "a" * 64},
        {"path": "/local/b.png", "name": "b.png", "hash": "b" * 64},
    ])
    back = ClipItem.from_wire(item.to_wire())
    assert back.signature() == item.signature(), "signature must survive the wire"
    assert [f["name"] for f in back.files] == ["a.txt", "b.png"]
    assert all(f["path"] == "" for f in back.files), "local paths must not travel"

    text = ClipItem(type="text", text="hello", hash=hash_text("hello"))
    assert ClipItem.from_wire(text.to_wire()).signature() == text.signature()
    assert item.signature() != ClipItem(type="file", files=item.files[:1]).signature()


def test_token():
    sharecv.TOKEN = ""
    assert token_ok("") and token_ok("anything"), "no token configured = open"
    sharecv.TOKEN = "secret"
    assert token_ok("secret") and not token_ok("") and not token_ok("wrong")
    sharecv.TOKEN = ""


if __name__ == "__main__":
    test_echo_suppression()
    test_wire_roundtrip()
    test_token()
    print("OK")
