from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Flask web application should use port 8000 per project convention
    app.run(host="0.0.0.0", port=8000)


