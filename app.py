from flask import Flask, request, jsonify, render_template
import os
from planner import AgentPlanner
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more verbosity
    format='%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s'
)

app = Flask(__name__, static_folder='static', template_folder='templates')

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/agent-planner', methods=['POST'])
def agent_planner():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is empty"}), 400

        required_fields = ['session_chat_history']
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({
                "error": f"Missing required fields: {', '.join(missing_fields)}"
            }), 400

        session_id = data.get('session_id', None)
        user_id = data.get('user_id', None)
        chat_history = data['session_chat_history']

        planner = AgentPlanner(chat_history, is_interactive=True, session_id=session_id, user_id=user_id)
        planner.run_planner()

        return jsonify({"assistant": planner.data.final_answer}), 200

    except Exception as e:
        logging.error("Exception occurred: %s", str(e))
        logging.error(traceback.format_exc())
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('FLASK_PORT', 5000)),
        debug=True
    )
