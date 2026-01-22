from flask import Flask, render_template, jsonify, request, send_file
import os
import uuid
import datetime
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- CONFIGURATION ---
MONGO_URI = os.environ.get('MONGO_URL_LOCAL')
UPLOAD_FOLDER = 'firmware_uploads'
ALLOWED_EXTENSIONS = {'bin'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- DATABASE ---
client = MongoClient(MONGO_URI)
db = client['consentiumWebApp']
boards_col = db['newboards']
users_col = db['users']
releases_col = db['firmwareReleases']

USERNAME = "debjyotichowdhury"


# --- HELPERS ---
def get_user_id():
    user = users_col.find_one({"username": USERNAME})
    return user.get("_id") if user else None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_magic_tag(file_path):
    START_MARKER = b"[[[VER:"
    END_MARKER = b"]]]"

    try:
        with open(file_path, "rb") as f:
            content = f.read()

            start_index = content.find(START_MARKER)
            if start_index == -1:
                print("Magic marker not found.")
                return None  # Return None, not a string

            version_start = start_index + len(START_MARKER)
            end_index = content.find(END_MARKER, version_start)

            if end_index == -1:
                print("End marker not found.")
                return None

            version_bytes = content[version_start:end_index]
            return version_bytes.decode('utf-8')

    except Exception as e:
        print(f"Error reading file: {e}")
        return None

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/dashboard/boards')
def get_boards():
    user_id = get_user_id()
    if not user_id:
        return jsonify([]), 401

    # Fetch boards
    user_boards = list(boards_col.find(
        {'userId': user_id},
        {'_id': False, 'userId': False, 'active_release_id': False}  # Exclude internal fields
    ))

    # Sort so online/active boards might come first if you handled that in logic,
    # but for now just returning the list
    return jsonify(user_boards)


@app.route('/api/dashboard/history/<board_key>')
def get_history(board_key):
    user_id = get_user_id()

    # 1. Verify ownership
    board = boards_col.find_one({'userId': user_id, 'boardKey': board_key})
    if not board:
        return jsonify({'error': 'Board not found'}), 404

    # 2. Get currently active release ID (The Pointer)
    # We assume the board document has a field 'active_release_id'
    active_release_id = board.get('active_release_id')

    # 3. Fetch history
    history = list(releases_col.find(
        {'board_key': board_key},
        {'_id': 1, 'version': 1, 'commit_hash': 1, 'description': 1, 'uploaded_at': 1}
    ).sort('uploaded_at', -1))

    # 4. Format for Frontend
    response_data = []
    for rel in history:
        # Convert ObjectId to string for JSON serialization
        rel_id_str = str(rel['_id'])

        response_data.append({
            'release_id': rel_id_str,
            'version': rel.get('version', 'Unknown'),
            'commit_hash': rel.get('commit_hash', '---'),
            'description': rel.get('description', ''),
            'timestamp': rel.get('uploaded_at').strftime("%d %b %Y, %H:%M"),
            'is_active': (rel_id_str == str(active_release_id))  # Boolean flag for UI
        })

    return jsonify(response_data)


@app.route('/api/dashboard/upload', methods=['POST'])
def upload_firmware():
    user_id = get_user_id()

    # 1. Extract Data
    board_key = request.form.get('board_key')
    description = request.form.get('description')
    file = request.files.get('file')

    # 2. Validations
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file'}), 400

    board = boards_col.find_one({'userId': user_id, 'boardKey': board_key})
    if not board:
        return jsonify({'error': 'Board not found or access denied'}), 403

    if not board.get('statusOTA', False):
        return jsonify({'error': 'OTA is disabled for this board'}), 400

    # 3. Save File
    filename = secure_filename(f"{board_key}_{uuid.uuid4().hex[:8]}.bin")
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    # 4. Extract Magic Tag
    detected_version = extract_magic_tag(file_path)
    if not detected_version:
        os.remove(file_path)  # Cleanup
        return jsonify({'error': 'Magic Tag [[[VER:x.x]]] not found in binary'}), 400

    # 5. Create Release Record
    new_release = {
        'board_key': board_key,
        'version': detected_version,
        'description': description,
        'filename': filename,
        'commit_hash': uuid.uuid4().hex[:7],  # Simulating a git hash
        'uploaded_at': datetime.datetime.now()
    }
    result = releases_col.insert_one(new_release)
    new_release_id = result.inserted_id

    # 6. Update Board Pointer (Activate this version)
    boards_col.update_one(
        {'boardKey': board_key},
        {'$set': {
            'active_release_id': new_release_id,
            'firmwareVersion': detected_version,  # Update display string
            'updateFirmware': True,  # Flag for ESP32 to check
            'updatedAt': datetime.datetime.now()
        }}
    )

    return jsonify({
        'success': True,
        'version': detected_version,
        'message': 'Firmware uploaded and activated'
    })


@app.route('/api/dashboard/rollback', methods=['POST'])
def rollback_firmware():
    user_id = get_user_id()
    data = request.json

    board_key = data.get('board_key')
    target_release_id = data.get('target_release_id')

    print(type(board_key))

    # 1. Verify Board
    board = boards_col.find_one({'userId': user_id, 'boardKey': board_key})
    if not board:
        return jsonify({'error': 'Board not found'}), 404

    # 2. Verify Release exists and belongs to this board
    try:
        target_rel = releases_col.find_one({'_id': ObjectId(target_release_id), 'board_key': board_key})
        if not target_rel:
            return jsonify({'error': 'Target release not found'}), 404
    except:
        return jsonify({'error': 'Invalid Release ID'}), 400

    # 3. Update Pointer
    boards_col.update_one(
        {'boardKey': board_key},
        {'$set': {
            'active_release_id': ObjectId(target_release_id),
            'firmwareVersion': target_rel['version'],
            'updateFirmware': True,
            'updatedAt': datetime.datetime.now()
        }}
    )

    return jsonify({'success': True, 'message': f"Rolled back to v{target_rel['version']}"})


@app.route('/firmware/version', methods=['GET'])
def get_firmware_version():
    board_key = request.args.get('boardKey')
    if not board_key:
        return "Missing boardKey", 400

    board_details = boards_col.find_one({'boardKey': board_key})

    if not board_details:
        return "Board not found", 404

    # FIX: Handle case where 'firmwareVersion' doesn't exist yet (New Board)
    firmware_version = board_details.get('firmwareVersion', '0.0')

    # Return string to ensure Flask doesn't crash on numbers
    return str(firmware_version)


@app.route('/firmware/bin', methods=['GET'])
def download_firmware():
    board_key = request.args.get('boardKey')
    if not board_key:
        return "Missing boardKey", 400

    board_details = boards_col.find_one({'boardKey': board_key})
    if not board_details:
        return "Board not found", 404

    active_release_id = board_details.get('active_release_id')

    # FIX: Handle case where no firmware has been pushed yet
    if not active_release_id:
        return "No firmware active for this board", 404

    # FIX: Use try-catch or None check for the release entry
    firmware_entry = releases_col.find_one({'_id': active_release_id, 'board_key': board_key})

    if not firmware_entry:
        return "Firmware release entry missing/deleted", 404

    filename = firmware_entry.get('filename')
    firmware_file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

    # FIX: Check if actual file exists on disk before sending
    if not os.path.exists(firmware_file_path):
        return "Binary file missing from server storage", 404

    return send_file(
        firmware_file_path,
        as_attachment=True,
        download_name=filename,
        mimetype='application/octet-stream'
    )


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=True, port=5050)