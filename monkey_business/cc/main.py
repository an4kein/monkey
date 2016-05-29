import os
import sys
from flask import Flask, request, abort, send_from_directory
from flask.ext import restful
from flask.ext.pymongo import PyMongo
from flask import make_response
import bson.json_util
import json
from datetime import datetime
import dateutil.parser
from connectors.vcenter import VCenterJob, VCenterConnector
from connectors.demo import DemoJob, DemoConnector

MONGO_URL = os.environ.get('MONGO_URL')
if not MONGO_URL:
    MONGO_URL = "mongodb://localhost:27017/monkeybusiness"

app = Flask(__name__)
app.config['MONGO_URI'] = MONGO_URL
mongo = PyMongo(app)

available_jobs = [VCenterJob, DemoJob]

active_connectors = {}

class Root(restful.Resource):
    def get(self):
        return {
            'status': 'OK',
            'mongo': str(mongo.db),
        }


class Job(restful.Resource):
    def get(self, **kw):
        id = kw.get('id')
        timestamp = request.args.get('timestamp')

        if (id):
            return mongo.db.job.find_one_or_404({"id": id})
        else:
            result = {'timestamp': datetime.now().isoformat()}

        find_filter = {}
        if None != timestamp:
            find_filter['modifytime'] = {'$gt': dateutil.parser.parse(timestamp)}
        result['objects'] = [x for x in mongo.db.job.find(find_filter)]
        return result

    def post(self, **kw):
        job_json = json.loads(request.data)

        job_json["modifytime"] = datetime.now()

        if job_json.has_key('pk'):
            job = mongo.db.job.find_one_or_404({"pk": job_json["pk"]})

            if "pending" != job.get("status"):
                res = {"status": "cannot change job at this state", "res" : 0}
                return res
            if "delete" == job_json["action"]:
                return mongo.db.job.delete_one({"pk": job_json["pk"]})

        # update job
        job_json["status"] = "pending"
        return mongo.db.job.update({"pk": job_json["pk"]},
                                   {"$set": job_json},
                                   upsert=True)

class Connector(restful.Resource):
    def get(self, **kw):
        type = request.args.get('type')
        if (type == 'VCenterConnector'):
            vcenter = VCenterConnector()
            properties = mongo.db.connector.find_one({"type": 'VCenterConnector'})
            if properties:
                vcenter.load_properties(properties)
            ret = vcenter.get_properties()
            ret["password"] = "" # for better security, don't expose password
            return ret
        return {}

    def post(self, **kw):
        settings_json = json.loads(request.data)
        if (settings_json.get("type") == 'VCenterConnector'):

            # preserve password
            properties = mongo.db.connector.find_one({"type": 'VCenterConnector'})
            if properties and (not settings_json.has_key("password") or not settings_json["password"]):
                settings_json["password"] = properties.get("password")

            return mongo.db.connector.update({"type": 'VCenterConnector'},
                                               {"$set": settings_json},
                                               upsert=True)

class JobCreation(restful.Resource):
    def get(self, **kw):
        jobtype = request.args.get('type')
        if not jobtype:
            res = []
            update_connectors()
            for con in available_jobs:
                if con.connector.__name__ in active_connectors:
                    res.append({"title": con.__name__, "$ref": "/jobcreate?type=" + con.__name__})
            return {"oneOf": res}

        job = None
        for jobclass in available_jobs:
            if jobclass.__name__ == jobtype:
                job = jobclass()

        if job and job.connector.__name__ in active_connectors.keys():
            properties = dict()
            job_prop = job.get_job_properties()

            for prop in job_prop:
                properties[prop] = dict({})
                if type(job_prop[prop][0]) is int:
                    properties[prop]["type"] = "number"
                elif type(job_prop[prop][0]) is bool:
                    properties[prop]["type"] = "boolean"
                else:
                    properties[prop]["type"] = "string"
                if job_prop[prop][1]:
                    properties[prop]["enum"] = list(active_connectors[job.connector.__name__].__getattribute__(job_prop[prop][1])())

            res = dict({
                "title": "%s Job" % jobtype,
                "type": "object",
                "options": {
                    "disable_collapse": True,
                    "disable_properties": True,
                },
                "properties": properties
            })
            return res

        return {}


def normalize_obj(obj):
    if obj.has_key('_id') and not obj.has_key('id'):
        obj['id'] = obj['_id']
        del obj['_id']

    for key,value in obj.items():
        if type(value) is bson.objectid.ObjectId:
            obj[key] = str(value)
        if type(value) is datetime:
            obj[key] = str(value)
        if type(value) is dict:
            obj[key] = normalize_obj(value)
        if type(value) is list:
            for i in range(0,len(value)):
                if type(value[i]) is dict:
                    value[i] = normalize_obj(value[i])
    return obj


def output_json(obj, code, headers=None):
    obj = normalize_obj(obj)
    resp = make_response(bson.json_util.dumps(obj), code)
    resp.headers.extend(headers or {})
    return resp


def refresh_connector_config(name):
    properties = mongo.db.connector.find_one({"type": name})
    if properties:
        active_connectors[name].load_properties(properties)


def update_connectors():
    for con in available_jobs:
        connector_name = con.connector.__name__
        if connector_name not in active_connectors:
            active_connectors[connector_name] = con.connector()

        if not active_connectors[connector_name].is_connected():
            refresh_connector_config(connector_name)
            try:
                app.logger.info("Trying to activate connector: %s" % connector_name)
                active_connectors[connector_name].connect()
            except Exception, e:
                active_connectors.pop(connector_name)
                app.logger.info("Error activating connector: %s, reason: %s" % (connector_name, e))



@app.route('/admin/<path:path>')
def send_admin(path):
    return send_from_directory('admin/ui', path)

DEFAULT_REPRESENTATIONS = {'application/json': output_json}
api = restful.Api(app)
api.representations = DEFAULT_REPRESENTATIONS

api.add_resource(Root, '/api')
api.add_resource(Job, '/job')
api.add_resource(Connector, '/connector')
api.add_resource(JobCreation, '/jobcreate')

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, ssl_context=('server.crt', 'server.key'))