import unittest
from src.fetcher.content_parser import parse_article_content

class ContentParserTests(unittest.TestCase):
    def test_navigation_does_not_consume_article_budget(self):
        story = 'The company launched a new marketing product. ' * 20
        html = '<html><nav>' + 'MENU ' * 1000 + '</nav><div class="td-post-content">' + story + '</div></html>'
        result = parse_article_content(html, 300)
        self.assertTrue(result.startswith('The company launched'))
        self.assertNotIn('MENU', result)

    def test_rss_fragment_preserved(self):
        self.assertEqual(parse_article_content('<p>New product <b>launch</b></p>'), 'New product launch')
