"""Hosted-browser lifecycle primitive; called only by an authenticated worker.

No public CDP port, persisted portal cookies, arbitrary URLs or automatic login.
Live portal selectors and the authenticated human-interaction gateway remain
deployment prerequisites, so this primitive is not exposed by server.py.
"""
from urllib.parse import urlparse

class HostedSession:
    def __init__(self, playwright, allowed_origin):
        parsed = urlparse(allowed_origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('','/'):
            raise ValueError('A fixed HTTPS portal origin is required.')
        self.origin = allowed_origin.rstrip('/')
        self.playwright = playwright
        self.browser = self.context = self.page = None
    async def open(self):
        self.browser = await self.playwright.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            self.context = await self.browser.new_context(accept_downloads=False,service_workers='block')
            async def restrict(route):
                url = urlparse(route.request.url)
                if f'{url.scheme}://{url.netloc}' == self.origin:
                    await route.continue_()
                else: await route.abort()
            await self.context.route('**/*',restrict)
            self.page = await self.context.new_page()
            await self.page.goto(self.origin,timeout=30000)
            return self.page
        except Exception:
            await self.close(); raise
    async def close(self):
        try:
            if self.context: await self.context.close()
        finally:
            if self.browser: await self.browser.close()
            self.browser = self.context = self.page = None
