#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
logging.basicConfig(format='%(asctime)s : %(filename)s : %(levelname)s : %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import os
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))

import json
import yaml
import math
import hashlib
from hashlib import sha256
import traceback
from datetime import datetime
from urllib.parse import urlparse

import requests
logging.getLogger('requests').setLevel(logging.INFO)

from flask import Flask, request, Response, redirect
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

from pymongo import MongoClient

with open(f'{SCRIPT_DIR}/config.yaml', 'r') as fp:
    config = yaml.load(fp.read(), Loader=yaml.FullLoader)

iiifhosting_user, iiifhosting_token = config['iiifhosting'].split(':')
atlas_endpoint = f'mongodb+srv://{config["atlas"]}/?retryWrites=true&w=majority'
referrer_whitelist = set(config['referrer_whitelist'])
baseurl = config['baseurl']

ingest_endpoint_iiifhosting = 'https://admin.iiifhosting.com/api/v1/ingest/'
placeholder_image = 'https://upload.wikimedia.org/wikipedia/commons/e/e0/PlaceholderLC.png'

from expiringdict import ExpiringDict
_cache = ExpiringDict(max_len=100, max_age_seconds=3600)

_db_connection = None
def connect_db():
    '''MongoDB connection'''
    global _db_connection
    if _db_connection is None:
        _db_connection = MongoClient(atlas_endpoint)['iiif']
    return _db_connection

def get_image_size(url, **kwargs):
    '''Image size required for IIIF Hosting ingest'''
    size = None
    try:
        resp = requests.head(
            url, 
            headers={'User-agent': 'Labs Python Client'}
        )
        headers = dict([(key.lower(),value) for key, value in resp.headers.items()])
        size = int(headers.get('content-length', headers.get('content_length')))
    except:
        try:
            size = 0
            with requests.get(url, stream=True) as response:
                size = sum(len(chunk) for chunk in response.iter_content(8196))
        except:
            logger.error(traceback.format_exc())
            logger.error(url)
    logger.info(f'get_image_size: url={url} size={size}')
    return size

def queue_image_for_iiifhosting(mdb, **kwargs):
    url = kwargs['url']
    refresh = str(kwargs.get('refresh', False)).lower() == 'true'
    image_data = mdb['images'].find_one({'_id': url})
    exists = image_data is not None and image_data['url'] and requests.head(image_data['url']).status_code == 200
    logger.info(f'queue_image_for_iiifhosting: url={url} image_data={image_data is not None} exists={exists} refresh={refresh}')
    if not exists or refresh:
        size = int(kwargs['size']) if 'size' in kwargs else get_image_size(url)
        name = kwargs['name'] if 'name' in kwargs else sha256(url.encode('utf-8')).hexdigest()
        logger.info(f'queue_image_for_iiifhosting: url={url} name={name} size={size}')
        if size:
            if image_data:
                mdb['images'].update_one({'_id': url}, {'$set': {
                    'status': 'submitted',
                    'submitted': datetime.utcnow().isoformat()
                }})
            else:
                mdb['images'].insert_one({
                    '_id': url,
                    'status': 'submitted',
                    'source_size': size,
                    'submitted': datetime.utcnow().isoformat(),
                    'external_id': url
                })
            data = {
                'email': iiifhosting_user,
                'secure_payload': iiifhosting_token,
                'files': [{
                    'id': url, 
                    'url': url, 
                    'name': name,
                    'size': size}]
            }
            # logger.info(json.dumps(data, indent=2))
            resp = requests.post(
                ingest_endpoint_iiifhosting,
                headers = {
                    'Content-type': 'application/json; charset=utf-8', 
                    'Accept': 'application/json'
                },
                data = json.dumps(data))
            if resp.status_code == 200 and resp.json().get('success') == 'Task created':
                mdb['images'].update_one({'_id': url}, {'$set': {'status': 'pending'}})
                return 'Processing', 202
            else:
                return resp.text, resp.status_code
        else:
            'Error: unable to determine image size', 400
    else:
        return image_data, 200

def get_image_data(mdb, url):
    return mdb['images'].find_one({'_id': url})

def make_iiif_image(mdb, **kwargs):
    queue_image_for_iiifhosting(mdb, **kwargs)

def to_isodate(s):
    return s # TODO: ensure date is in proper ISO format

def add_image_data_to_manifest(manifest, image_data):
    logger.debug(f'add_image_data_to_manifest: image_data={image_data}')
    if 'url' in image_data:
        image_data['url'] = image_data['url'].replace('http:', 'https:')
        manifest['sequences'][0]['canvases'][0]['images'][0]['resource'] = {
            '@id': image_data['external_id'],
            '@type': 'dcTypes:Image',
            'format': 'image/jpeg',
            'height': image_data['height'],
            'width': image_data['width']
        }
        if 'status' not in image_data or image_data['status'] == 'done':
            manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service'] = {
                '@context': 'http://iiif.io/api/image/2/context.json',
                '@id': image_data['url'][:-1],
                'profile': 'http://iiif.io/api/image/2/level2.json'
            }
            manifest['thumbnail'] = f'{image_data["url"]}full/150,/0/default.jpg'
        else:
            if 'thumbnail' in manifest:
                del manifest['thumbnail']
    logger.debug(json.dumps(manifest, indent=2))
    return manifest

def update_manifests_with_image_data(mdb, image_data):
    image_data['url'] = image_data['url'].replace('http:', 'https:')
    _filter = {'sequences.canvases.images.resource.@id': {'$eq': image_data['external_id']}}
    # logger.info(f'update_manifests_with_image_data: image_data={image_data}')
    # logger.info(image_data['external_id'])
    cursor = mdb['manifests'].find(_filter)
    for manifest in cursor:
        # logger.info(f'manifest={manifest}')
        manifest = add_image_data_to_manifest(manifest, image_data)
        mdb['manifests'].replace_one({'_id': manifest['_id']}, manifest)   

def make_manifest_v2_1_1(mdb, mid, image_data, dryrun=False, **kwargs):
    '''Create an IIIF presentation v2.1.1 manifest'''
    manifest = {
        '@context': 'http://iiif.io/api/presentation/2/context.json',
        '@id': f'{baseurl}/manifest/{mid}',
        '@type': 'sc:Manifest',
        'label': kwargs.get('label', '')  ,
        'metadata': metadata(**kwargs),
        'sequences': [{
            '@id': f'{baseurl}/sequence/{mid}',
            '@type': 'sc:Sequence',
            'canvases': [{
                '@id': f'{baseurl}/canvas/{mid}',
                '@type': 'sc:Canvas',
                'label': kwargs.get('label', ''),
                'height': 3000,
                'width': 3000,
                'images': [{
                    '@type': 'oa:Annotation',
                    'motivation': 'sc:painting',
                    'resource': {
                        '@id': kwargs['url'],
                    },
                    'on': f'{baseurl}/canvas/{mid}'
                }]
            }]
        }]
    }
    if image_data and 'url' in image_data:
        manifest = add_image_data_to_manifest(manifest, image_data)
        manifest['thumbnail'] = f'{image_data["url"]}full/150,/0/default.jpg'
    else:
        logger.info(f'No valid image data: {image_data}')

    for prop in kwargs:
        if prop.lower() in ('attribution', 'description', 'label', 'license', 'logo', 'navDate'):
            manifest[prop.lower()] = kwargs[prop]
            if prop.lower() == 'label':
                manifest['sequences'][0]['canvases'][0]['label'] = kwargs[prop]

    manifest['_id'] = mid
    logger.debug(json.dumps(manifest, indent=2))
    if dryrun:
        return manifest
    else:
        if mdb['manifests'].find_one({'_id': mid}):
            mdb['manifests'].replace_one({'_id': manifest['_id']}, manifest)  
        else: 
            mdb['manifests'].insert_one(manifest)
        return mdb['manifests'].find_one({'_id': mid})

def metadata(**kwargs):
    md = []
    for prop in kwargs:
        if prop == 'navDate':
            md.append({ 'label': prop, 'value': to_isodate(kwargs['navDate']) })
        elif prop == 'url':
            md.append({ 'label': 'image-source-url', 'value': kwargs[prop] })
        else:
            md.append({ 'label': prop, 'value': kwargs[prop] })
    return md

def update_manifest(mdb, manifest, image_data, **kwargs):
    manifest['metadata'] = metadata(**kwargs)
    for prop in kwargs:
        if prop.lower() in ('attribution', 'description', 'label', 'license', 'logo', 'navDate'):
            manifest[prop.lower()] = kwargs[prop]
            if prop.lower() == 'label':
                manifest['sequences'][0]['canvases'][0]['label'] = kwargs[prop]
    if image_data:
        manifest = add_image_data_to_manifest(manifest, image_data)
    mdb['manifests'].replace_one({'_id': manifest['_id']}, manifest)        
    return mdb['manifests'].find_one({'_id': manifest['_id']})

def _source(url):
    _url = urlparse(url)
    if _url.hostname == 'raw.githubusercontent.com':
        path_elems = [elem for elem in _url.path.split('/') if elem]
        acct, repo, ref = path_elems[:3]
        path = f'/{"/".join(path_elems[3:])}'
        logger.info(f'GitHub image: hostname={_url.hostname} acct={acct} repo={repo} ref={ref} path={path}')
        return f'https://{_url.hostname}/{acct}/{repo}/{ref}/{path}'
    else:
        return url

@app.route('/gp-proxy/<path:path>', methods=['GET', 'HEAD'])
def gp_proxy(path):
    gp_url = f'https://plants.jstor.org/seqapp/adore-djatoka/resolver?url_ver=Z39.88-2004&svc_id=info:lanl-repo/svc/getRegion&svc_val_fmt=info:ofi/fmt:kev:mtx:jpeg2000&svc.format=image/jpeg&rft_id=/{path}'
    if request.method in ('HEAD'):
        resp = requests.get(gp_url, headers = {'User-Agent': 'JSTOR Labs'})
        _cache[gp_url] = resp.content
        if resp.status_code == 200:
            res = Response('', 204, content_type='image/jpeg')
            res.headers.add('Content-Length', str(len(resp.content)))
            res.headers.add('Content_Length', str(len(resp.content)))
            return res
    else:
        content = _cache.get(gp_url)
        if content is None:
            resp = requests.get(gp_url, headers = {'User-Agent': 'JSTOR Labs'})
            if resp.status_code == 200:
                content = resp.content
        if content:
            return (content, 200, {'Content-Type': 'image/jpeg', 'Content-Length': len(content)})

@app.route('/manifest/<path:path>', methods=['GET'])
@app.route('/manifest/', methods=['OPTIONS', 'POST', 'PUT'])
def manifest(path=None):
    referrer = '.'.join(urlparse(request.referrer).netloc.split('.')[-2:]) if request.referrer else None
    can_mutate = referrer is None or referrer.startswith('localhost') or referrer in referrer_whitelist
    if request.method == 'OPTIONS':
        return ('', 204)
    elif request.method in ('HEAD', 'GET'):
        mid = path
        args = dict([(k, request.args.get(k)) for k in request.args])
        if 'url' in args:
            args['url'] = args['url'].replace(' ', '%20')
        refresh = args.get('refresh', 'false').lower() in ('', 'true')
        mdb = connect_db()
        manifest = mdb['manifests'].find_one({'_id': mid})
        logger.info(f'manifest: method={request.method} mid={mid} found={manifest is not None} refresh={refresh} referrer={referrer} can_mutate={can_mutate}')
        if manifest:
            etag = hashlib.md5(json.dumps(manifest.get('metadata',{}), sort_keys=True).encode()).hexdigest()
            # headers = {**cors_headers, **{'ETag': etag}}
            headers = {'ETag': etag}
            if request.method == 'GET':
                del manifest['_id']
                if 'service' in manifest['sequences'][0]['canvases'][0]['images'][0]['resource']:
                    manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['profile'] = 'http://iiif.io/api/image/2/level2.json'
                source_url = manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['@id']
                if refresh:
                    make_iiif_image(mdb, url=source_url)
                return (manifest, 200, headers)
            else: # HEAD
                return ('', 204, headers)
        else:
            return 'Not found', 404
    elif request.method == 'POST':

        mdb = connect_db()
        input_data = request.json
        dryrun = request.json.pop('dryrun', 'false').lower() in ('true', '')
        if 'url' in input_data:
            input_data['url'] = input_data['url'].replace(' ', '%20')
        source = _source(input_data['url'])
        info_json_url = input_data.get('iiif')

        # make manifest id using hash of url
        mid = hashlib.sha256(source.encode()).hexdigest()

        manifest = mdb['manifests'].find_one({'_id': mid})

        refresh = str(input_data.pop('refresh', False)).lower() in ('', 'true')

        logger.info(f'manifest: method={request.method} source={source} mid={mid} found={manifest is not None} refresh={refresh} referrer={referrer} can_mutate={can_mutate} dryrun={dryrun}')

        if manifest:
            
            # logger.info(f'manifest={manifest}')
            if can_mutate:
                image_data = None
                if refresh or \
                    'service' not in manifest['sequences'][0]['canvases'][0]['images'][0]['resource'] or \
                    'cdn.visual-essays.app' in manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['@id']:
                    if info_json_url:
                        resp = requests.get(info_json_url, headers = {'Accept': 'application/json'})
                        if resp.status_code == 200:
                            iiif_info = resp.json()
                            logger.debug(json.dumps(iiif_info, indent=2))
                            size = '1000,' if iiif_info['width'] >= iiif_info['height'] else ',1000'
                            image_data = {
                                'external_id': f'{iiif_info["@id"]}/full/{size}/0/default.jpg',
                                'url': f'{iiif_info["@id"]}/',
                                'height': iiif_info['height'],
                                'width': iiif_info['width']
                            }
                    else:
                        image_data = get_image_data(mdb, source)
                        # logger.info(f'image_data={image_data}')
                        if refresh or image_data is None or image_data['status'] != 'done':
                            make_iiif_image(mdb, refresh=True, **input_data)
                else:
                    image_data = None
                manifest_md_hash = hashlib.md5(json.dumps(manifest.get('metadata',{}), sort_keys=True).encode()).hexdigest()
                input_data_md_hash = hashlib.md5(json.dumps(metadata(**input_data), sort_keys=True).encode()).hexdigest()
                if (image_data is not None) or (manifest_md_hash != input_data_md_hash):
                    manifest = update_manifest(mdb, manifest, image_data, **input_data)
            else:
                if 'service' in manifest['sequences'][0]['canvases'][0]['images'][0]['resource']:
                    manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['profile'] = 'http://iiif.io/api/image/2/level2.json'

        else:
            if not can_mutate:
                return ('Not authorized', 403)

            image_data = get_image_data(mdb, source)
            logger.debug(f'image_data={image_data}')
            if (refresh or image_data is None) and 'iiif' in input_data:
                resp = requests.get(info_json_url, headers = {'Accept': 'application/json'})
                logger.debug(f'{info_json_url} {resp.status_code}')
                if resp.status_code == 200:
                    iiif_info = resp.json()
                    logger.debug(json.dumps(iiif_info, indent=2))
                    size = '1000,' if iiif_info['width'] >= iiif_info['height'] else ',1000'
                    image_data = {
                        'external_id': f'{iiif_info["@id"]}/full/{size}/0/default.jpg',
                        'url': f'{iiif_info["@id"]}/',
                        'height': iiif_info['height'],
                        'width': iiif_info['width']
                    }
            if not image_data:
                make_iiif_image(mdb, **input_data)
            manifest = make_manifest_v2_1_1(mdb, mid, image_data, dryrun, **input_data)
        return manifest, 200
    
    elif request.method == 'PUT':
        if not can_mutate:
            return ('Not authorized', 403)

        mdb = connect_db()
        input_data = request.json
        source = _source(input_data['url'])
        mid = hashlib.sha256(source.encode()).hexdigest()
        manifest = mdb['manifests'].find_one({'_id': mid})
        manifest = update_manifest(mdb, manifest, **input_data)
        if 'service' in manifest['sequences'][0]['canvases'][0]['images'][0]['resource']:
            manifest['sequences'][0]['canvases'][0]['images'][0]['resource']['service']['profile'] = 'http://iiif.io/api/image/2/level2.json'
        return manifest, 200

@app.route('/create-iiif-image/', methods=['POST'])
@app.route('/create-iiif-image', methods=['POST'])
def create_iiif_image():
    return queue_image_for_iiifhosting(connect_db(), **request.json)

@app.route('/service-endpoint/<path:url>', methods=['GET'])
def service_endpoint(url):
    logger.info(f'service-endpoint: url={url}')
    mdb = connect_db()
    image_data = mdb['images'].find_one({'_id':url})
    logger.info(image_data)
    return image_data if image_data else ('Not found', 404)

@app.route('/iiifhosting-webhook', methods=['GET', 'POST'])
def iiifhosting_webhook():
    if request.method == 'GET':
        kwargs = dict([(k, request.args.get(k)) for k in request.args])
        logger.info(f'iiifhosting-webhook: qargs={kwargs}')
    if request.method == 'POST':
        image_data = request.json
        logger.info(f'iiifhosting-webhook: image_data={json.dumps(image_data)}')
        # if image_data['status'] == 'done':
        mdb = connect_db()
        found = mdb['images'].find_one({'_id': image_data['external_id']})
        logger.info(f'found={found} status={image_data["status"]}')
        if found:
            if image_data['status'] == 'deleted':
                to_delete = mdb['images'].find_one({'image_id': image_data['image_id']})
                resp = (mdb['images'].delete_one({'image_id': image_data['image_id']}))
            else:
                mdb['images'].update_one(
                    {'_id': image_data['external_id']},
                    {'$set': {
                        'status': image_data['status'],
                        'created': datetime.utcnow().isoformat(),
                        'image_id': image_data['image_id'] if 'image_id' in image_data else image_data['external_id'],
                        'url': image_data['url'],
                        'height': image_data['height'],
                        'width': image_data['width']
                    }
                })
        else:
            mdb['images'].insert_one({
                '_id': image_data['external_id'],
                'status': image_data['status'],
                'source_size': image_data['source_size'],
                'created': datetime.utcnow().isoformat(),
                'image_id': image_data['image_id'] if 'image_id' in image_data else image_data['external_id'],
                'url': image_data['url'],
                'height': image_data['height'],
                'width': image_data['width']
            })
        update_manifests_with_image_data(mdb, image_data)
    return 'OK', 200

def _calc_region_and_size(image_data, args, type='thumbnail'):
        
    im_width = int(image_data['width'])
    im_height = int(image_data['height'])

    width = height = None

    if 'size' in args:
        size = args.get('size', 'full').replace('x',',').replace('X',',')
        if ',' not in size:
            size = f'{size},'
        width, height = [int(arg) if arg.isdecimal() else None for arg in size.split(',')]
    else:
        if 'width' in args: width = int(args['width'])
        if 'height' in args: height = int(args['height'])

    if width == None and height == None:
        width = 400 if type == 'thumbnail' else 1000
        height = 260 if type == 'thumbnail' else 400
    else:
        if not width: width = round(im_height/height * im_width)
        if not height: height = round(width/im_width * im_height)
    aspect = width / height

    if aspect > 1:
        x = 0
        w = im_width
        h = math.ceil(im_width / aspect)
        y = math.ceil((im_height-h) / 2)
    else:
        y = 0
        h = im_height
        w = math.ceil(im_height * aspect)
        x = math.ceil((im_width-w) / 2)

    region = f'{x},{y},{w},{h}'
    size = f'{width},{height}'

    logger.info(f'_calc_region_and_size: width={width} height={height} aspect={aspect} im_width={im_width} im_height={im_height} region={region} size={size}')
    return region, size

@app.route('/thumbnail/', methods=['GET'])
@app.route('/thumbnail/', methods=['OPTIONS', 'POST', 'PUT'])
@app.route('/banner/', methods=['GET'])
@app.route('/banner/', methods=['OPTIONS', 'POST', 'PUT'])
def thumbnail():
    action = request.path.split('/')[1]
    referrer = '.'.join(urlparse(request.referrer).netloc.split('.')[-2:]) if request.referrer else None
    can_mutate = referrer is None or referrer.startswith('localhost') or referrer in referrer_whitelist
    if request.method == 'OPTIONS':
        return ('', 204)
    elif request.method in ('HEAD', 'GET'):
        args = dict([(k, request.args.get(k)) for k in request.args])
        refresh = args.get('refresh', 'false').lower() in ('', 'true')
        region = args.get('region', 'full')
        size = args.get('size', 'full')
        rotation = args.get('rotation', '0')
        quality = args.get('quality', 'default')
        format = args.get('format', 'jpg')

        logger.info(f'thumbnail: method={request.method} action={action} region={region} size={size} referrer={referrer} can_mutate={can_mutate} args={args}')
        if 'url' in args:
            source = _source(args['url'])
            mdb = connect_db()
            image_data = get_image_data(mdb, source)
            if image_data and not refresh:
                if region == 'full':
                    region, size = _calc_region_and_size(image_data, args, action)
                # logger.info(json.dumps(image_data, indent=2))
                thumbnail_url = f'{image_data["url"].replace("http:","https:")}{region}/{size}/{rotation}/{quality}.{format}'
                logger.info(thumbnail_url)
                return redirect(thumbnail_url)
                '''
                resp = requests.get(thumbnail_url)
                if resp.status_code == 200:
                    content = resp.content
                    if content:
                        return (content, 200, {'Content-Type': 'image/jpeg', 'Content-Length': len(content)})
                '''
            else:
                if can_mutate:
                    queue_image_for_iiifhosting(mdb, url=source)
                    placeholder = get_image_data(mdb, placeholder_image)
                    if region == 'full':
                        region, size = _calc_region_and_size(placeholder, args, action)
                    thumbnail_url = f'{placeholder["url"].replace("http:","https:")}{region}/{size}/{rotation}/{quality}.{format}'
                    return redirect(thumbnail_url)
                    '''
                    resp = requests.get(thumbnail_url)
                    if resp.status_code == 200:
                        content = resp.content
                        if content:
                            return (content, 200, {'Content-Type': 'image/jpeg', 'Content-Length': len(content)})
                    '''
                else:
                    return 'Not found', 404
    return 'Bad Request', 400

defaults = {'port': 8080}
def usage():
    print(f'{sys.argv[0]} [hl:p:]')
    print(f'   -h --help         Print help message')
    print(f'   -l --loglevel     Logging level (default=warning)')
    print(f'   -p --port         Port (default={defaults["port"]})')

if __name__ == '__main__':
    kwargs = defaults
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hl:p:', ['help', 'loglevel', 'port'])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(str(err)) # will print something like "option -a not recognized"
        usage()
        sys.exit(2)

    for o, a in opts:
        if o in ('-l', '--loglevel'):
            loglevel = a.lower()
            if loglevel in ('error',): logger.setLevel(logging.ERROR)
            elif loglevel in ('warn','warning'): logger.setLevel(logging.INFO)
            elif loglevel in ('info',): logger.setLevel(logging.INFO)
            elif loglevel in ('debug',): logger.setLevel(logging.DEBUG)
        elif o in ('-p', '--port'):
            kwargs['port'] = int(a)
        elif o in ('-h', '--help'):
            usage()
            sys.exit()
        else:
            assert False, 'unhandled option'

    app.run(debug=True, port=kwargs['port'], host='0.0.0.0')