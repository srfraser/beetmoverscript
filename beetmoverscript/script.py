#!/usr/bin/env python
"""Beetmover script
"""
import asyncio
import logging
import os
import sys
import traceback
import mimetypes
import aiohttp
import boto3

from scriptworker.client import get_task
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException, ScriptWorkerRetryException
from scriptworker.utils import retry_async, raise_future_exceptions

from beetmoverscript.constants import MIME_MAP, RELEASE_BRANCHES, CACHE_CONTROL_MAXAGE
from beetmoverscript.task import (validate_task_schema, add_balrog_manifest_to_artifacts,
                                  get_upstream_artifacts, get_initial_release_props_file,
                                  add_checksums_to_artifacts,
                                  add_release_props_to_artifacts,
                                  get_task_bucket, get_task_action,
                                  validate_bucket_paths)
from beetmoverscript.utils import (load_json, get_hash, get_release_props,
                                   generate_beetmover_manifest, get_size,
                                   alter_unpretty_contents)

log = logging.getLogger(__name__)


async def push_to_nightly(context):
    # determine artifacts to beetmove
    context.artifacts_to_beetmove = get_upstream_artifacts(context)

    # find release properties and make a copy in the artifacts directory
    release_props_file = get_initial_release_props_file(context)
    context.release_props = get_release_props(release_props_file)

    # generate beetmover mapping manifest
    mapping_manifest = generate_beetmover_manifest(context)

    # perform another validation check against the bucket path
    validate_bucket_paths(context.bucket, mapping_manifest['s3_bucket_path'])

    # some files to-be-determined via script configs need to have their
    # contents pretty named, so doing it here before even beetmoving begins
    blobs = context.config.get('blobs_needing_prettynaming_contents', [])
    alter_unpretty_contents(context, blobs, mapping_manifest)

    # balrog_manifest is written and uploaded as an artifact which is used by
    # a subsequent balrogworker task in the release graph. Balrogworker uses
    # this manifest to submit release blob info (e.g. mar filename, size, etc)
    context.balrog_manifest = list()

    # Used as a staging area to generate balrog_manifest, so that all the
    # completes and partials for a release end up in the same data structure
    context.raw_balrog_manifest = dict()

    # the checksums manifest is written and uploaded as an artifact which is
    # used by a subsequent signing task and again by another beetmover task to
    # upload it to S3
    context.checksums = dict()

    # for each artifact in manifest
    #   a. map each upstream artifact to pretty name release bucket format
    #   b. upload to corresponding S3 location
    await move_beets(context, context.artifacts_to_beetmove, mapping_manifest)

    #  write balrog_manifest to a file and add it to list of artifacts
    add_balrog_manifest_to_artifacts(context)
    # determine the correct checksum filename and generate it, adding it to
    # the list of artifacts afterwards
    add_checksums_to_artifacts(context)
    # add release props file to later be used by beetmover jobs than upload
    # the checksums file
    add_release_props_to_artifacts(context, release_props_file)


async def push_to_releases(context):
    raise NotImplementedError("Push to releases logic has not been added yet")


async def push_to_staging(context):
    raise NotImplementedError("Push to staging logic has not been added yet")


action_map = {
    'push-to-nightly': push_to_nightly,
    # push to candidates is at this point identical to push_to_nightly
    'push-to-candidates': push_to_nightly,
    'push-to-releases': push_to_releases,
    'push-to-staging': push_to_staging
}


# async_main {{{1
async def async_main(context):
    # determine the task and make a quick validation check against its schema
    context.task = get_task(context.config)  # e.g. $cfg['work_dir']/task.json
    validate_task_schema(context)

    # determine the task bucket and action
    context.bucket = get_task_bucket(context.task, context.config)
    context.action = get_task_action(context.task, context.config)

    if action_map.get(context.action):
        await action_map[context.action](context)
    else:
        log.critical("Unknown action {}!".format(context.action))
        sys.exit(3)

    log.info('Success!')


async def move_beets(context, artifacts_to_beetmove, manifest):
    beets = []
    for locale in artifacts_to_beetmove:
        for artifact in artifacts_to_beetmove[locale]:
            source = artifacts_to_beetmove[locale][artifact]
            artifact_pretty_name = manifest['mapping'][locale][artifact]['s3_key']
            destinations = [os.path.join(manifest["s3_bucket_path"],
                                         dest) for dest in
                            manifest['mapping'][locale][artifact]['destinations']]

            balrog_manifest = manifest['mapping'][locale][artifact].get('update_balrog_manifest')
            # For partials
            from_buildid = manifest['mapping'][locale][artifact].get('from_buildid')
            beets.append(
                asyncio.ensure_future(
                    move_beet(context, source, destinations, locale=locale,
                              update_balrog_manifest=balrog_manifest,
                              from_buildid=from_buildid,
                              artifact_pretty_name=artifact_pretty_name)
                )
            )
    await raise_future_exceptions(beets)

    # Fix up balrog manifest. We need an entry with both completes and
    # partials, which is why we store up the data from each moved beet
    # and collate it now.
    for locale in context.raw_balrog_manifest:
        balrog_entry = enrich_balrog_manifest(context, locale)
        balrog_entry['completeInfo'] = context.raw_balrog_manifest[locale]['completeInfo']
        if 'partialInfo' in context.raw_balrog_manifest[locale]:
            balrog_entry['partialInfo'] = context.raw_balrog_manifest[locale]['partialInfo']
        context.balrog_manifest.append(balrog_entry)


async def move_beet(context, source, destinations, locale,
                    update_balrog_manifest, from_buildid, artifact_pretty_name):
    await retry_upload(context=context, destinations=destinations, path=source)

    if context.checksums.get(artifact_pretty_name) is None:
        context.checksums[artifact_pretty_name] = {
            algo: get_hash(source, algo) for algo in context.config['checksums_digests']
        }
        context.checksums[artifact_pretty_name]['size'] = get_size(source)

    if update_balrog_manifest:
        context.raw_balrog_manifest.setdefault(locale, {})
        if from_buildid:
            component = 'partialInfo'
        else:
            component = 'completeInfo'
        context.raw_balrog_manifest[locale].setdefault(component, [])
        context.raw_balrog_manifest[locale][component].append(generate_balrog_info(context, artifact_pretty_name,
                                                                                   locale, destinations, from_buildid))


def generate_balrog_info(context, artifact_pretty_name, locale, destinations, from_buildid=None):
    release_props = context.release_props
    checksums = context.checksums

    url = "{prefix}/{s3_key}".format(prefix="https://archive.mozilla.org",
                                     s3_key=destinations[0])

    data = {
        "hash": checksums[artifact_pretty_name][release_props["hashType"]],
        "size": checksums[artifact_pretty_name]['size'],
        "url": url
    }
    if from_buildid:
        data["from_buildid"] = from_buildid
    return data


def enrich_balrog_manifest(context, locale):
    release_props = context.release_props

    url_replacements = []
    if release_props["branch"] in RELEASE_BRANCHES:
        url_replacements.append(['http://archive.mozilla.org/pub',
                                 'http://download.cdn.mozilla.net/pub'])

    return {
        "tc_nightly": True,
        "appName": release_props["appName"],
        "appVersion": release_props["appVersion"],
        "branch": release_props["branch"],
        "buildid": release_props["buildid"],
        "extVersion": release_props["appVersion"],
        "hashType": release_props["hashType"],
        "locale": locale if not locale == 'multi' else 'en-US',
        "platform": release_props["stage_platform"],
        "url_replacements": url_replacements
    }


async def retry_upload(context, destinations, path):
    # TODO rather than upload twice, use something like boto's bucket.copy_key
    #   probably via the awscli subproc directly.
    # For now, this will be faster than using copy_key() as boto would block
    uploads = []
    for dest in destinations:
        uploads.append(
            asyncio.ensure_future(
                upload_to_s3(context=context, s3_key=dest, path=path)
            )
        )
    await raise_future_exceptions(uploads)


async def put(context, url, headers, abs_filename, session=None):
    session = session or context.session
    with open(abs_filename, "rb") as fh:
        async with session.put(url, data=fh, headers=headers, compress=False) as resp:
            log.info("put {}: {}".format(abs_filename, resp.status))
            response_text = await resp.text()
            log.info(response_text)
            if resp.status not in (200, 204):
                raise ScriptWorkerRetryException(
                    "Bad status {}".format(resp.status),
                )
    return resp


async def upload_to_s3(context, s3_key, path):
    app = context.release_props['appName'].lower()
    api_kwargs = {
        'Bucket': context.config['bucket_config'][context.bucket]['buckets'][app],
        'Key': s3_key,
        'ContentType': mimetypes.guess_type(path)[0]
    }
    headers = {
        'Content-Type': mimetypes.guess_type(path)[0],
        'Cache-Control': 'public, max-age=%d' % CACHE_CONTROL_MAXAGE,
    }
    creds = context.config['bucket_config'][context.bucket]['credentials']
    s3 = boto3.client('s3', aws_access_key_id=creds['id'], aws_secret_access_key=creds['key'],)
    url = s3.generate_presigned_url('put_object', api_kwargs, ExpiresIn=1800, HttpMethod='PUT')

    await retry_async(put, args=(context, url, headers, path),
                      retry_exceptions=(Exception, ),
                      kwargs={'session': context.session})


# main {{{1
def usage():
    print("Usage: {} CONFIG_FILE".format(sys.argv[0]), file=sys.stderr)
    sys.exit(1)


def setup_config(config_path):
    if config_path is None:
        if len(sys.argv) != 2:
            usage()
        config_path = sys.argv[1]
    context = Context()
    context.config = {}
    context.config.update(load_json(path=config_path))
    return context


def setup_logging():
    log_level = logging.DEBUG
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=log_level
    )
    logging.getLogger("taskcluster").setLevel(logging.WARNING)


def setup_mimetypes():
    mimetypes.init()
    # in py3 we must exhaust the map so that add_type is actually invoked
    list(map(
        lambda ext_mimetype: mimetypes.add_type(ext_mimetype[1], ext_mimetype[0]), MIME_MAP.items()
    ))


def main(name=None, config_path=None):
    if name not in (None, '__main__'):
        return
    context = setup_config(config_path)
    setup_logging()
    setup_mimetypes()

    loop = asyncio.get_event_loop()
    conn = aiohttp.TCPConnector(limit=context.config['aiohttp_max_connections'])
    with aiohttp.ClientSession(connector=conn) as session:
        context.session = session
        try:
            loop.run_until_complete(async_main(context))
        except ScriptWorkerTaskException as exc:
            traceback.print_exc()
            sys.exit(exc.exit_code)
    loop.close()


main(name=__name__)
