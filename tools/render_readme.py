"""Generuje stronę README z tego samego źródła co GitHub."""
import argparse
import hashlib
import re
from pathlib import Path
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]

def render():
    source = (ROOT/'README.md').read_text(encoding='utf8')
    content = MarkdownIt('commonmark', {'html':False}).enable('table').render(source)
    content = re.sub(r'href="([^"#:/][^"]*)"',
                     lambda match: 'href="https://github.com/Ankudev/Monitor_OSD_Community/blob/main/' + match.group(1) + '"', content)
    article = '<main class="readme-main"><article class="readme-card">'+content+'</article></main>'
    digest = hashlib.sha256(source.encode()).hexdigest()
    return '<!-- Generowane przez tools/render_readme.py; SHA-256: '+digest+' -->\n'+(ROOT/'tools/readme-template.html').read_text(encoding='utf8').replace('{{CONTENT}}', article)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = ROOT/'web/readme.html'
    content = render()
    if args.check:
        if target.read_text(encoding='utf8') != content:
            raise SystemExit('README na stronie wymaga ponownego wygenerowania.')
    else:
        target.write_text(content, encoding='utf8')

if __name__ == '__main__':
    main()
