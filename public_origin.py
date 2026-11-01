"""Przekierowanie dawnych nazw hosta i adresy nasłuchiwania usługi."""
import os
import re
from urllib.parse import urlsplit

from aiohttp import web


def bind_hosts(default):
    hosts = [value.strip() for value in os.environ.get('PGE_WEB_HOSTS', default).split(',')]
    if not all(hosts):
        raise ValueError('PGE_WEB_HOSTS musi zawierać niepuste adresy nasłuchiwania.')
    return hosts


def redirect_middleware():
    hosts = {value.strip().lower() for value in os.environ.get('PGE_REDIRECT_HOSTS', '').split(',') if value.strip()}
    origin = os.environ.get('PGE_PUBLIC_ORIGIN', '').rstrip('/')
    if hosts:
        target = urlsplit(origin)
        if (target.scheme != 'https' or not target.hostname or target.username or target.password
                or target.path or target.query or target.fragment or target.port not in (None, 443)):
            raise ValueError('Przekierowanie wymaga poprawnego adresu HTTPS w PGE_PUBLIC_ORIGIN.')
        if any(not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', host) for host in hosts):
            raise ValueError('PGE_REDIRECT_HOSTS przyjmuje nazwy hostów bez portów i ścieżek.')
        if target.hostname.lower() in hosts:
            raise ValueError('Docelowy host nie może przekierowywać sam na siebie.')

    @web.middleware
    async def redirect(request, handler):
        host = request.headers.get('Host', '').lower().removesuffix(':443').removesuffix(':80').rstrip('.')
        # Istniejące subskrypcje push nadal wymagają skryptu z pierwotnej domeny.
        worker = request.path == '/osd-push/OneSignalSDKWorker.js'
        if host in hosts and not worker:
            return web.Response(
                status=301 if request.method in ('GET', 'HEAD') else 308,
                headers={'Location': origin + request.rel_url.raw_path_qs,
                         'Cache-Control': 'public, max-age=300'},
            )
        return await handler(request)

    return redirect
