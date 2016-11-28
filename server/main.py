import logging
import os
import sys
import threading
import json
import time

import flask
from flask import Flask, render_template, request
from flask_socketio import SocketIO
from flask_cors import CORS

from opentrons import robot
from opentrons.util import trace
from opentrons.util.vector import VectorEncoder

sys.path.insert(0, os.path.abspath('..'))  # NOQA
from server.helpers import helpers, process_manager


TEMPLATES_FOLDER = os.path.join(helpers.get_frozen_root() or '', 'templates')
STATIC_FOLDER = os.path.join(helpers.get_frozen_root() or '', 'static')
BACKGROUND_TASKS = {}

app = Flask(__name__,
            static_folder=STATIC_FOLDER,
            template_folder=TEMPLATES_FOLDER
            )


CORS(app)
app.jinja_env.autoescape = False
app.config['ALLOWED_EXTENSIONS'] = set(['json', 'py'])
socketio = SocketIO(app, async_mode='gevent')

filename = "x"
last_modified = "y"


def emit_notifications(notifications, _type):
    for notification in notifications:
        socketio.emit('event', {
            'name': 'notification',
            'text': notification,
            'type': _type
        })


def notify(info):
    s = json.dumps(info, cls=VectorEncoder)
    socketio.emit('event', json.loads(s))


trace.EventBroker.get_instance().add(notify)


@app.route("/")
def welcome():
    return render_template("index.html")


@app.route('/dist/<path:filename>')
def script_loader(filename):
    root = helpers.get_frozen_root() or app.root_path
    scripts_root_path = os.path.join(root, 'templates', 'dist')
    return flask.send_from_directory(
        scripts_root_path, filename, mimetype='application/javascript'
    )


@app.route("/app_version")
def app_version():
    return flask.jsonify({
        'version': os.environ.get("appVersion")
    })


@app.route("/upload", methods=["POST"])
def upload():
    global filename
    global last_modified

    file = request.files.get('file')
    filename = file.filename
    last_modified = request.form.get('lastModified')

    if not file:
        return flask.jsonify({
            'status': 'error',
            'data': 'File expected'
        })

    extension = file.filename.split('.')[-1].lower()

    api_response = None
    if extension == 'py':
        api_response = helpers.load_python(file.stream, file)
    elif extension == 'json':
        api_response = helpers.load_json(file.stream)
    else:
        return flask.jsonify({
            'status': 'error',
            'data': '{} is not a valid extension. Expected'
            '.py or .json'.format(extension)
        })

    if len(api_response['errors']) > 0:
        # TODO: no need for both http response and socket emit
        emit_notifications(api_response['errors'], 'danger')
        status = 'error'
        calibrations = []
    else:
        # TODO: no need for both http response and socket emit
        emit_notifications(
            ["Successfully uploaded {}".format(file.filename)], 'success')
        status = 'success'
        calibrations = helpers.create_step_list()

    return flask.jsonify({
        'status': status,
        'data': {
            'errors': api_response['errors'],
            'warnings': api_response['warnings'],
            'calibrations': calibrations,
            'fileName': filename,
            'lastModified': last_modified
        }
    })


@app.route("/load")
def load():
    status = "success"
    try:
        calibrations = helpers.update_step_list()
    except Exception as e:
        emit_notifications([str(e)], "danger")
        status = 'error'

    return flask.jsonify({
        'status': status,
        'data': {
            'calibrations': calibrations,
            'fileName': filename,
            'lastModified': last_modified
        }
    })


def _run_commands():
    start_time = time.time()
    global robot

    api_response = {'errors': [], 'warnings': []}

    try:
        robot.resume()
        robot.home()
        robot.run(caller='ui')
    except Exception as e:
        api_response['errors'] = [str(e)]

    api_response['warnings'] = robot.get_warnings() or []
    api_response['name'] = 'run exited'
    end_time = time.time()
    emit_notifications(api_response['warnings'], 'warning')
    emit_notifications(api_response['errors'], 'danger')
    seconds = end_time - start_time
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    run_time = "%d:%02d:%02d" % (hours, minutes, seconds)
    result = "Run complete in {}".format(run_time)
    emit_notifications([result], 'success')
    socketio.emit('event', {'name': 'run-finished'})


@app.route("/run", methods=["GET"])
def run():
    thread = threading.Thread(target=_run_commands)
    thread.start()

    return flask.jsonify({
        'status': 'success',
        'data': {}
    })


@app.route("/pause", methods=["GET"])
def pause():
    result = robot.pause()
    emit_notifications(['Protocol paused'], 'info')

    return flask.jsonify({
        'status': 'success',
        'data': result
    })


@app.route("/resume", methods=["GET"])
def resume():
    result = robot.resume()
    emit_notifications(['Protocol resumed'], 'info')

    return flask.jsonify({
        'status': 'success',
        'data': result
    })


@app.route("/cancel", methods=["GET"])
def stop():
    result = robot.stop()
    emit_notifications(['Protocol stopped'], 'info')

    return flask.jsonify({
        'status': 'success',
        'data': result
    })


@app.route("/robot/serial/list")
def get_serial_ports_list():
    global robot
    return flask.jsonify({
        'ports': robot.get_serial_ports_list()
    })


@app.route("/robot/serial/is_connected")
def is_connected():
    global robot
    return flask.jsonify({
        'is_connected': robot.is_connected(),
        'port': robot.get_connected_port()
    })


@app.route("/robot/get_coordinates")
def get_coordinates():
    global robot
    return flask.jsonify({
        'coords': robot._driver.get_position().get("target")
    })


@app.route("/robot/diagnostics")
def diagnostics():
    global robot
    return flask.jsonify({
        'diagnostics': robot.diagnostics()
    })


@app.route("/robot/versions")
def get_versions():
    global robot
    return flask.jsonify({
        'versions': robot.versions()
    })


@app.route("/robot/serial/connect", methods=["POST"])
def connect_robot():
    port = request.json.get('port')
    options = request.json.get('options', {'limit_switches': False})

    status = 'success'
    data = None

    global robot
    try:
        robot.connect(port, options=options)
    except Exception as e:
        # any robot version incompatibility will be caught here
        robot.disconnect()
        status = 'error'
        data = str(e)
        if "versions are incompatible" in data:
            data += ". To upgrade, go to docs.opentrons.com"
        emit_notifications([data], 'danger')

    return flask.jsonify({
        'status': status,
        'data': data
    })


@app.route("/robot/serial/disconnect")
def disconnect_robot():
    status = 'success'
    data = None

    global robot
    try:
        robot.disconnect()
        emit_notifications(["Successfully disconnected"], 'info')
    except Exception as e:
        status = 'error'
        data = str(e)
        emit_notifications([data], 'danger')

    return flask.jsonify({
        'status': status,
        'data': data
    })


@app.route("/instruments/placeables")
def placeables():
    try:
        data = helpers.update_step_list()
    except Exception as e:
        emit_notifications([str(e)], 'danger')

    return flask.jsonify({
        'status': 'success',
        'data': data
    })


@app.route('/home/<axis>')
def home(axis):
    status = 'success'
    result = ''
    try:
        if axis == 'undefined' or axis == '' or axis.lower() == 'all':
            result = robot.home(enqueue=False)
        else:
            result = robot.home(axis, enqueue=False)
        emit_notifications(["Successfully homed"], 'info')
    except Exception as e:
        result = str(e)
        status = 'error'
        emit_notifications([result], 'danger')

    return flask.jsonify({
        'status': status,
        'data': result
    })


@app.route('/jog', methods=["POST"])
def jog():
    coords = request.json

    status = 'success'
    result = ''
    try:
        if coords.get("a") or coords.get("b"):
            result = robot._driver.move_plunger(mode="relative", **coords)
        else:
            result = robot.move_head(mode="relative", **coords)
    except Exception as e:
        result = str(e)
        status = 'error'
        emit_notifications([result], 'danger')

    return flask.jsonify({
        'status': status,
        'data': result
    })


@app.route('/move_to_slot', methods=["POST"])
def move_to_slot():
    status = 'success'
    result = ''
    try:
        slot = request.json.get("slot")
        slot = robot._deck[slot]

        slot_x, slot_y, _ = slot.from_center(
            x=-1, y=0, z=0, reference=robot._deck)
        _, _, robot_max_z = robot._driver.get_dimensions()

        robot.move_head(z=robot_max_z)
        robot.move_head(x=slot_x, y=slot_y)
    except Exception as e:
        result = str(e)
        status = 'error'
        emit_notifications([result], 'danger')

    return flask.jsonify({
        'status': status,
        'data': result
    })


@app.route('/move_to_container', methods=["POST"])
def move_to_container():
    slot = request.json.get("slot")
    name = request.json.get("label")
    axis = request.json.get("axis")
    try:
        instrument = robot._instruments[axis.upper()]
        container = robot._deck[slot].get_child_by_name(name)
        instrument.move_to(container[0].bottom(), enqueue=False)
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/pick_up_tip', methods=["POST"])
def pick_up_tip():
    global robot
    try:
        axis = request.json.get("axis")
        instrument = robot._instruments[axis.upper()]
        instrument.reset_tip_tracking()
        instrument.pick_up_tip(enqueue=False)
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/drop_tip', methods=["POST"])
def drop_tip():
    global robot
    try:
        axis = request.json.get("axis")
        instrument = robot._instruments[axis.upper()]
        instrument.return_tip(enqueue=False)
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/move_to_plunger_position', methods=["POST"])
def move_to_plunger_position():
    position = request.json.get("position")
    axis = request.json.get("axis")
    try:
        instrument = robot._instruments[axis.upper()]
        instrument.motor.move(instrument.positions[position])
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/aspirate', methods=["POST"])
def aspirate_from_current_position():
    axis = request.json.get("axis")
    try:
        # this action mimics 1.2 app experience
        # but should be re-thought to take advantage of API features
        instrument = robot._instruments[axis.upper()]
        robot.move_head(z=20, mode='relative')
        instrument.motor.move(instrument.positions['blow_out'])
        instrument.motor.move(instrument.positions['bottom'])
        robot.move_head(z=-20, mode='relative')
        instrument.motor.move(instrument.positions['top'])
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/dispense', methods=["POST"])
def dispense_from_current_position():
    axis = request.json.get("axis")
    try:
        # this action mimics 1.2 app experience
        # but should be re-thought to take advantage of API features
        instrument = robot._instruments[axis.upper()]
        instrument.motor.move(instrument.positions['blow_out'])
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route('/set_max_volume', methods=["POST"])
def set_max_volume():
    volume = request.json.get("volume")
    axis = request.json.get("axis")
    try:
        instrument = robot._instruments[axis.upper()]
        instrument.set_max_volume(int(volume))
        msg = "Max volume set to {0}ul on the {1} axis".format(volume, axis)
        emit_notifications([msg], 'success')
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    return flask.jsonify({
        'status': 'success',
        'data': ''
    })


@app.route("/calibrate_placeable", methods=["POST"])
def calibrate_placeable():
    name = request.json.get("label")
    axis = request.json.get("axis")
    try:
        helpers.calibrate_placeable(name, axis)
        calibrations = helpers.update_step_list()
        emit_notifications(
            ['Saved {0} for the {1} axis'.format(name, axis)], 'success')
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    # TODO change calibration key to steplist
    return flask.jsonify({
        'status': 'success',
        'data': {
            'name': name,
            'axis': axis,
            'calibrations': calibrations
        }
    })


@app.route("/calibrate_plunger", methods=["POST"])
def calibrate_plunger():
    position = request.json.get("position")
    axis = request.json.get("axis")
    try:
        helpers.calibrate_plunger(position, axis)
        emit_notifications(
            ['Saved {0} on the {1} pipette'.format(position, axis)], 'success')
    except Exception as e:
        emit_notifications([str(e)], 'danger')
        return flask.jsonify({
            'status': 'error',
            'data': str(e)
        })

    calibrations = helpers.update_step_list()

    # TODO change calibration key to steplist
    return flask.jsonify({
        'status': 'success',
        'data': {
            'position': position,
            'axis': axis,
            'calibrations': calibrations
        }
    })


# NOTE(Ahmed): DO NOT REMOVE socketio requires a confirmation from the
# front end that a connection was established, this route does that.
@socketio.on('connected')
def on_connect():
    app.logger.info('Socketio connected to front end...')


@app.before_request
def log_before_request():
    logger = logging.getLogger('opentrons-app')
    log_msg = "[BR] {method} {url} | {data}".format(
        method=request.method,
        url=request.url,
        data=request.data,
    )
    logger.info(log_msg)


@app.after_request
def log_after_request(response):
    response.direct_passthrough = False
    if response.mimetype in ('text/html', 'application/javascript'):
        return response
    logger = logging.getLogger('opentrons-app')
    log_msg = "[AR] {data}".format(data=response.data)
    logger.info(log_msg)
    return response


if __name__ == "__main__":
    data_dir = os.environ.get('APP_DATA_DIR', os.getcwd())
    IS_DEBUG = os.environ.get('DEBUG', '').lower() == 'true'
    if not IS_DEBUG:
        process_manager.run_once(data_dir)

    lg = logging.getLogger('opentrons-app')
    lg.info('Starting Flask Server')
    [app.logger.addHandler(handler) for handler in lg.handlers]

    socketio.run(
        app,
        debug=False,
        logger=False,
        use_reloader=False,
        log_output=False,
        engineio_logger=False,
        port=31950
    )
