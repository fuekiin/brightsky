import json
import logging

import click
import uvicorn
from fastapi.testclient import TestClient
from huey.consumer_options import ConsumerConfig

from brightsky import db, tasks
from brightsky.utils import parse_date
from brightsky.web import app
from brightsky.worker import huey


def dump_records(it):
    for record in it:
        print(json.dumps(record, default=str))


def migrate_callback(ctx, param, value):
    if value:
        db.migrate()


def parse_date_arg(ctx, param, value):
    if not value:
        return
    return parse_date(value)


@click.group()
@click.option(
    '--migrate', help='Migrate database before running command.',
    is_flag=True, is_eager=True, expose_value=False, callback=migrate_callback)
def cli():
    pass


@cli.command()
def migrate():
    """Apply all pending database migrations."""
    db.migrate()


@cli.command()
@click.argument(
    'targets',
    required=True,
    nargs=-1,
    metavar='TARGET [TARGET ...]',
)
def parse(targets):
    for target in targets:
        tasks.parse(target)


@cli.command()
@click.option(
    '--enqueue/--no-enqueue', default=False,
    help='Enqueue updated files for processing by the worker')
def poll(enqueue):
    """Detect updated files on DWD Open Data Server."""
    files = tasks.poll(enqueue=enqueue)
    if not enqueue:
        dump_records(files)


@cli.command()
def clean():
    """Clean expired forecast and observations from database."""
    tasks.clean()


@cli.command()
@click.option('--workers', default=3, type=int, help='Number of threads')
def work(workers):
    """Start brightsky worker."""
    huey.flush()
    config = ConsumerConfig(worker_type='thread', workers=workers)
    config.validate()
    consumer = huey.create_consumer(**config.values)
    consumer.run()


@cli.command()
@click.option('--bind', default='127.0.0.1:5000', help='Bind address')
@click.option(
    '--reload/--no-reload', default=False,
    help='Reload server on source code changes')
@click.option('--workers', default=1, type=int, help='Number of workers')
def serve(bind, reload, workers):
    """Start brightsky API webserver."""
    host, port = bind.rsplit(':', 1)
    uvicorn.run(
        'brightsky.web:app',
        host=host,
        port=int(port),
        reload=reload,
        workers=workers,
    )


@cli.command(context_settings={'ignore_unknown_options': True})
@click.argument('endpoint')
@click.argument('parameters', nargs=-1, type=click.UNPROCESSED)
def query(endpoint, parameters):
    """Query API and print JSON response.

    Parameters must be supplied as --name value or --name=value. See
    https://brightsky.dev/docs/ for the available endpoints and arguments.

    \b
    Examples:
    python -m brightsky query weather --lat 52 --lon 7.6 --date 2018-08-13
    python -m brightsky query current_weather --lat=52 --lon=7.6
    """
    for route in app.routes:
        if route.path == f'/{endpoint}':
            break
    else:
        raise click.UsageError(f"Unknown endpoint '{endpoint}'")
    logging.getLogger().setLevel(logging.WARNING)
    with TestClient(app) as client:
        resp = client.get(f'/{endpoint}', params=_parse_params(parameters))
    print(json.dumps(resp.json()))


def _parse_params(parameters):
    # I'm sure there's a function in click or argparse somewhere that does this
    # but I can't find it
    usage = "Supply API parameters as --name value or --name=value"
    params = {}
    param_name = None
    for param in parameters:
        if param_name is None:
            if not param.startswith('--'):
                raise click.UsageError(usage)
            param = param[2:]
            if '=' in param:
                name, value = param.split('=', 1)
                params[name] = value
            else:
                param_name = param
        else:
            params[param_name] = param
            param_name = None
    if param_name is not None:
        raise click.UsageError(usage)
    return params


@cli.command(name='radar3d-work')
def radar3d_work():
    """Start the nano radar3d worker (sweep polling + gridding)."""
    from brightsky.radar3d.ingest import run_forever
    run_forever()


@cli.command(name='radar3d-grid')
@click.argument('directory')
def radar3d_grid(directory):
    """Grid saved sweep_vol_z files from DIRECTORY into the frame store."""
    from brightsky.radar3d.ingest import grid_directory
    for cycle in grid_directory(directory):
        print(cycle.isoformat())


@cli.command(name='push-serve')
@click.option('--bind', default='127.0.0.1:5001', help='Bind address')
@click.option(
    '--forwarded-allow-ips', default='127.0.0.1',
    help='Proxies trusted for X-Forwarded-For (Traefik in production)')
def push_serve(bind, forwarded_allow_ips):
    """Start the nano push API (registration, activity tokens, health)."""
    host, port = bind.rsplit(':', 1)
    uvicorn.run(
        'brightsky.push.api:app',
        host=host,
        port=int(port),
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


@cli.command(name='push-send')
@click.argument('device_id')
@click.option(
    '--kind', default='alert',
    type=click.Choice(['alert', 'start', 'update', 'end']),
    help='A notification, or a Live Activity start/update/end')
@click.option('--title', default='nano Testmitteilung')
@click.option('--body', default='Hallo vom Push-Server.')
@click.option(
    '--payload', 'payload_file', type=click.File(),
    help='Send this JSON instead of the built-in example')
def push_send(device_id, kind, title, body, payload_file):
    """Send a hand-made push to a registered device (design §12.2)."""
    import asyncio
    from brightsky.push.handmade import send_handmade
    payload = json.load(payload_file) if payload_file else None
    result = asyncio.run(
        send_handmade(device_id, kind, title, body, payload))
    print(json.dumps(result))


@cli.command(name='push-work')
def push_work():
    """Start the nano push worker (warnings and forecast loops)."""
    import asyncio
    from brightsky.push.worker import run
    asyncio.run(run())
