import os
from string import ascii_letters, digits
import random
import asyncio
import aiohttp
import aiohttp_jinja2
import jinja2
from aiohttp import web
import bcrypt
import secrets
from datetime import datetime
import uuid
import traceback
import json

routes = web.RouteTableDef()


async def fetch_remote_data(app):
    try:
        while True:
            try:
                # clients update
                ips = ["ingest-readsb:150"]
                print("Fetching data from", ips)
                for ip in ips:
                    clients = []
                    receivers = []
                    async with app["session"].get(f"http://{ip}/clients.json") as resp:
                        data = await resp.json()
                        clients += data["clients"]
                        print(len(clients), "clients")

                    async with app["session"].get(
                        f"http://{ip}/receivers.json"
                    ) as resp:
                        data = await resp.json()
                        for receiver in data["receivers"]:
                            lat, lon = round(receiver[8], 2), round(receiver[9], 2)
                            receivers.append([lat, lon])
                print(len(receivers), "receivers")

                app["beast_clients"] = beast_clients_to_set(clients)
                app["beast_receivers"] = receivers

                # mlat update
                print("Fetching mlat data")
                async with app["session"].get(
                    "http://mlat-mlat-server:150/sync.json"
                ) as resp:
                    data = await resp.json()
                print("Fetched mlat sync.json")
                app["mlat_sync_json"] = anonymize_mlat_data(app, data)
                app["mlat_totalcount_json"] = {
                    "0A": len(app["mlat_sync_json"]),
                    "UPDATED": datetime.now().strftime("%a %b %d %H:%M:%S UTC %Y"),
                }

                # mlat clients.json
                print("Fetching mlat clients.json")
                async with app["session"].get(
                    "http://mlat-mlat-server:150/clients.json"
                ) as resp:
                    data = await resp.json()
                app["mlat_clients"] = data

                print("Looped..")
                await asyncio.sleep(1)
            except Exception as e:
                traceback.print_exc()
                print("Error in background task:", e)
    except asyncio.CancelledError:
        print("Background task cancelled")


def beast_clients_to_set(clients):
    clients_set = set()
    for client in clients:
        hex = client[0]
        ip = client[1].split()[1]
        kbps = client[2]
        conn_time = client[3]
        msg_s = client[4]
        position_s = client[5]
        reduce_signal = client[6]
        positions = client[8]

        clients_set.add(
            (hex, ip, kbps, conn_time, msg_s, position_s, reduce_signal, positions)
        )
    return clients_set


def mlat_clients_to_list(clients, ip=None):
    clients_list = []
    keys_to_copy = (
        "user privacy connection peer_count bad_sync_timeout outlier_percent".split()
    )
    for name, client in clients.items():
        print(client)
        if ip is not None and client["source_ip"] != ip:
            continue
        clients_list.append({key: client[key] for key in keys_to_copy if key in client})
    return clients_list


def anonymize_mlat_data(app, data):
    sanitized_data = {}
    for name, value in data.items():
        sanitised_peers = {}
        for peer, peer_value in value["peers"].items():
            sanitised_peers[cachehash(app, peer)] = peer_value

        sanitized_data[cachehash(app, name)] = {
            "lat": value["lat"],
            "lon": value["lon"],
            "peers": sanitised_peers,
        }

    return sanitized_data


def get_clients_per_ip(clients, ip: str) -> list:
    return [client for client in clients if client[1] == ip]


def cachehash(app, name):
    # Only hash UUIDs
    try:
        uuid.UUID(name)
        salt = b"$2b$04$OGq0aceBoTGtzkUfT0FGme"
        if name not in app["mlat_cached_names"]:
            print("Hashing...")
            hash = bcrypt.hashpw(name.encode(), salt).decode()
            candidate = "".join([c for c in hash if c.isalnum()])[-13:]
            app["mlat_cached_names"][name] = name[0:3] + "_" + candidate[-13:]
    except ValueError:
        return name

    return app["mlat_cached_names"][name]


@routes.get("/")
async def index(request):
    ip = request.headers["X-Original-Forwarded-For"]
    clients_beast = get_clients_per_ip(request.app["beast_clients"], ip)
    clients_mlat = mlat_clients_to_list(request.app["mlat_clients"], ip)
    context = {
        "clients_beast": clients_beast,
        "clients_mlat": clients_mlat,
        "own_mlat_clients": len(clients_mlat),
        "ip": ip,
        "len_beast": len(request.app["beast_clients"]),
        "len_mlat": len(request.app["mlat_clients"]),
    }
    response = aiohttp_jinja2.render_template("index.html", request, context)
    return response

    # Return a template index.html with the clients, pass the clients to the template which is a index.html file


@routes.get("/api/0/receivers")
async def receivers(request):
    return web.json_response(request.app["beast_receivers"])


@routes.get("/api/0/mlat-server/0A/sync.json")
async def mlat_receivers(request):
    return web.json_response(request.app["mlat_sync_json"])


@routes.get("/api/0/mlat-server/totalcount.json")
async def mlat_totalcount_json(request):
    return web.json_response(app["mlat_totalcount_json"])


@routes.get("/api/0/uuid")
async def get_uuid(request):
    return web.text_response(str(uuid.uuid4()))


@routes.get("/metrics")
async def metrics(request):
    metrics = [
        "adsb_api_beast_total_receivers {}".format(len(request.app["beast_receivers"])),
        "adsb_api_beast_total_clients {}".format(len(request.app["beast_clients"])),
        "adsb_api_mlat_total {}".format(len(request.app["mlat_sync_json"])),
    ]
    return web.Response(text="\n".join(metrics))


@routes.get("/api/0/me")
async def api_me(request):
    ip = request.headers["X-Original-Forwarded-For"]
    beast_clients_set = get_clients_per_ip(request.app["beast_clients"], ip)
    beast_clients_list = []
    for client in beast_clients_set:
        beast_clients_list.append(
            {
                "type": "beast",
                "hex": client[0],
                "kbps": client[2],
                "connected_seconds": client[3],
                "positions": client[7],
                "messages_per_second": client[4],
                "positions_per_second": client[5],
            }
        )
    mlat_clients = mlat_clients_to_list(request.app["mlat_clients"], ip)
    response = {
        "feeding": {
            "beast": len(beast_clients_list) > 0,
            "mlat": len(mlat_clients) > 0,
        },
        "clients": {
            "beast": beast_clients_list,
            "mlat": mlat_clients,
        },
        "ip": ip,
    }
    # Format the response pretty
    return web.json_response(response, dumps=lambda x: json.dumps(x, indent=4))


async def background_tasks(app):
    app["fetch_remote_data"] = asyncio.create_task(fetch_remote_data(app))

    yield

    app["fetch_remote_data"].cancel()
    await app["fetch_remote_data"]


# aiohttp server
app = web.Application()
app.add_routes(routes)
app["beast_clients"] = set()
app["beast_receivers"] = []
app["mlat_sync_json"] = {}
app["mlat_totalcount_json"] = {}
app["mlat_cached_names"] = {}
app["mlat_clients"] = {}


async def aiohttp_session_setup(app):
    timeout = aiohttp.ClientTimeout(total=5.0, connect=1.0, sock_connect=1.0)
    app["session"] = aiohttp.ClientSession(timeout=timeout)


async def aiohttp_session_cleanup(app):
    await app["session"].close()
    asyncio.sleep(0)


# add background task
app.cleanup_ctx.append(background_tasks)
app.on_startup.append(aiohttp_session_setup)
app.on_cleanup.append(aiohttp_session_cleanup)


if __name__ == "__main__":
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader("/app/templates"))
    web.run_app(app, host="0.0.0.0", port=80)
