import unittest

from wechat_cli.commands.favorites import _parse_fav_url
from wechat_cli.core.messages import _extract_app_message_url


class LinkUrlTests(unittest.TestCase):
    def test_extracts_url_from_appmsg(self):
        xml = (
            '<msg><appmsg><title>文章</title><type>5</type>'
            '<url>https://example.com/a</url></appmsg></msg>'
        )
        self.assertEqual(_extract_app_message_url(xml), 'https://example.com/a')

    def test_returns_empty_without_url(self):
        xml = '<msg><appmsg><title>无链接</title></appmsg></msg>'
        self.assertEqual(_extract_app_message_url(xml), '')

    def test_returns_empty_for_plain_text(self):
        self.assertEqual(_extract_app_message_url('hello'), '')

    def test_extracts_url_after_stripping_group_sender_prefix(self):
        xml = (
            'wxid_sender:\n<msg><appmsg><title>文章</title><type>5</type>'
            '<url>https://example.com/group</url></appmsg></msg>'
        )
        self.assertEqual(_extract_app_message_url(xml, is_group=True), 'https://example.com/group')
        self.assertEqual(_extract_app_message_url(xml, is_group=False), '')


class FavoriteUrlTests(unittest.TestCase):
    def test_extracts_article_link_from_favorite_xml(self):
        xml = (
            '<favitem type="5"><source>'
            '<link>https://example.com/a?x=1&amp;y=2</link>'
            '</source></favitem>'
        )
        self.assertEqual(_parse_fav_url(xml), 'https://example.com/a?x=1&y=2')

    def test_returns_empty_without_link_or_on_invalid_xml(self):
        self.assertEqual(_parse_fav_url('<favitem><desc>文本</desc></favitem>'), '')
        self.assertEqual(_parse_fav_url('not xml'), '')
