from aiohttp import web
import asyncio
from os import path
from pprint import pprint
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
import aiohttp_jinja2
import jinja2
from aiohttp_oauth2 import oauth2_app
from aiohttp_session import get_session, setup as session_setup, SimpleCookieStorage
from aiohttp_remotes import setup as remotes_setup, XForwardedStrict
import json
from aiohttp.abc import AbstractAccessLogger
import logging
from datetime import datetime
from typing import Dict, Any
import random
import uuid


with open('settings.json', 'r') as f:
    cfg = json.load(f)

db_client = MongoClient(cfg['mongodb']['host'], cfg['mongodb']['port'])
db = db_client[cfg['mongodb']['db']]
db_options = db['options']
db_boards = db['boards']
db_sessions = db['sessions']


async def get_session_data(request):
    session = await get_session(request)
    try:
        sid = ObjectId(session.get('id'))
    except InvalidId:
        return None
    if sid is None:
        return None
    entry = db_sessions.find_one({'_id': sid})
    if entry is None:
        return None
    return entry


async def session_validation(request) -> Dict:
    session = await get_session_data(request)
    allowed = db_options.find_one({}).get('allowed')
    final = {
        "user": session,
        "logged_in": False,
        "allowed": False
    }
    if session is not None:
        final['logged_in'] = True
        if session.get('id') in allowed:
            final['allowed'] = True
    return final


async def handle_on_login(request: web.Request, token: Dict[str, Any]) -> 'web.Response':
    session = await get_session(request)
    # invalidate old session if exist for user?
    user = {}
    async with request.app['session'].get(
            "https://discordapp.com/api/users/@me",
            headers={"Authorization": f"Bearer {token.get('access_token')}"}
    ) as r:
        js = await r.json()
        user = {
            'id': int(js.get('id')),
            'username': js.get('username'),
            'locale': js.get('locale'),
            'discriminator': js.get('discriminator'),
            'avatar': js.get('avatar')
        }
        result = db_sessions.insert_one(user)
        session['id'] = str(result.inserted_id)
    return web.HTTPTemporaryRedirect(location="/")


async def handle_logout(request):
    session = await get_session(request)
    sid = session.get('id')
    if sid is not None:
        db_sessions.delete_one({'_id': ObjectId(sid)})
    session.invalidate()
    return web.HTTPTemporaryRedirect(location="/")


def generate_new(user_id):
    opt = db_options.find_one({}).get('options')
    final = {
        "user_id": user_id,
        "solved": False,
        "board": [],
        "created": datetime.utcnow(),
        "current": True
    }
    ch = random.sample(opt, 25)
    for x in range(5):
        cur = []
        for y in range(5):
            cur.append({"title": ch.pop(),
                        "checked": False,
                        "idx": x*5 + y})
        final['board'].append(cur)
    db_boards.update_many({'user_id': user_id}, {'$set': {'current': False}})
    db_boards.insert_one(final)
    return final


@aiohttp_jinja2.template("index.html.j2")
async def handle_index(request):
    return await session_validation(request)


async def handle_new(request):
    final = await session_validation(request)
    if final.get('allowed'):
        generate_new(final.get('user').get('id'))
    return web.HTTPTemporaryRedirect(location="/")


@aiohttp_jinja2.template("board.html.j2")
async def handle_board(request):
    final = await session_validation(request)
    final['board'] = None
    if final.get('allowed'):
        user = final.get('user')
        entry = db_boards.find_one({'user_id': user.get('id'), 'current': True})
        if entry is None:
            entry = generate_new(user.get('id'))
        final['board'] = entry.get('board')
    return final


@aiohttp_jinja2.template("board.html.j2")
async def handle_flip(request):
    final = await session_validation(request)
    idx = int(request.match_info['idx'])
    if final.get('allowed'):
        entry = db_boards.find_one({'user_id': final.get('user').get('id'), 'current': True})
        for row in entry['board']:
            for e in row:
                if idx == e.get('idx'):
                    e['checked'] = not e['checked']
        db_boards.replace_one({'_id': entry.get('_id')}, entry)
        final['board'] = entry.get('board')
    return final


class AccessLogger(AbstractAccessLogger):

    def log(self, request, response, time):
        self.logger.info(f'[{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}] '
                         f'{request.remote} '
                         f'"{request.method} {request.rel_url}" '
                         f'done in {time}s: {response.status} '
                         f'- "{request.headers.get("User-Agent")}"')


if __name__ == '__main__':
    app = web.Application()
    loop = asyncio.get_event_loop()
    if cfg.get('proxy', {}).get('enabled', False):
        loop.run_until_complete(remotes_setup(app, XForwardedStrict([cfg.get('proxy', {}).get('trusted')])))
    session_setup(app, SimpleCookieStorage())
    aiohttp_jinja2.setup(app,
                         loader=jinja2.FileSystemLoader(str(path.join(path.dirname(__file__), 'res/templates/'))))
    dc = cfg["discord"]
    auth_app = oauth2_app(client_id=dc['client_id'],
                          client_secret=dc['client_secret'],
                          authorize_url="https://discordapp.com/api/oauth2/authorize",
                          token_url="https://discordapp.com/api/oauth2/token",
                          scopes=["identify"],
                          json_data=False,
                          on_login=handle_on_login)
    app.add_subapp("/login/discord/", auth_app)
    app.router.add_static('/static/',
                          path=str(path.join(path.dirname(__file__), 'res/static/')),
                          name='static')
    app.add_routes([web.get('/', handle_index),
                    web.get('/new/', handle_new),
                    web.get('/logout/', handle_logout),
                    web.get('/ajax/board/', handle_board),
                    web.get('/ajax/board/flip/{idx}', handle_flip)])
    app.logger.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler('access.log')])
    web.run_app(app, port=cfg.get('port'), access_log_class=AccessLogger)
