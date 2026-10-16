# A tiny Flask app with startup setup

```bash
pip install flask
```

Save this as `app.py`:

```python
from flask import Flask

app = Flask(__name__)

@app.before_first_request
def setup():
    app.config["GREETING"] = "Hello"

@app.route("/")
def index():
    return app.config.get("GREETING", "Hi") + ", world!"

if __name__ == "__main__":
    app.run(port=5000)
```

Start it with `python app.py` and open http://127.0.0.1:5000/ to see the greeting.
