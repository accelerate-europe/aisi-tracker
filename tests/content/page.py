"""The saved page as the content step reads it: what a reader sees, without inert markup.

The saved index.html also holds the page template (<template id="page-template">), the copy the page script
hydrates from. Its contents are never shown, but an HTML parser reads them like the rest of the page, so a check
would count its group rows or cards twice. Every content check reads the page with them removed."""

from bs4 import BeautifulSoup


def without_templates(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for template in soup.find_all("template"):
        template.decompose()
    return str(soup)
