from __future__ import annotations

from app.jobs.poll_news import _parse_rss
from app.util import hostname_matches_source


def test_hostname_matches_source_accepts_subdomains() -> None:
    assert hostname_matches_source("news.bbc.co.uk", "bbc.co.uk") is True
    assert hostname_matches_source("bbc.co.uk", "bbc.co.uk") is True
    assert hostname_matches_source("www.bbc.com", "bbc.com") is True
    assert hostname_matches_source("evil.com", "bbc.co.uk") is False


def test_parse_rss_uses_guid_when_link_empty() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Test story</title>
        <link></link>
        <guid isPermaLink="true">https://npr.org/sections/test/2024/01/01/story</guid>
        <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
        <description>Summary</description>
      </item>
    </channel></rss>"""
    items = _parse_rss(xml)
    assert len(items) == 1
    assert items[0]["link"] == "https://npr.org/sections/test/2024/01/01/story"
    assert hostname_matches_source("npr.org", "npr.org")


def test_parse_rss_atom_prefers_alternate_link() -> None:
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Atom item</title>
        <link rel="self" href="https://api.example.test/feed/entry-1"/>
        <link rel="alternate" href="https://www.example.test/news/1"/>
        <updated>2024-01-01T12:00:00Z</updated>
        <summary>Hi</summary>
      </entry>
    </feed>"""
    items = _parse_rss(xml)
    assert len(items) == 1
    assert items[0]["link"] == "https://www.example.test/news/1"
