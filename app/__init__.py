from flask import Flask, send_from_directory
import os


def create_app():
    app = Flask(__name__, static_folder='../frontend', static_url_path='')

    from app.routes import bp as api_bp
    app.register_blueprint(api_bp)

    # Serve the front-end (single page)
    @app.route('/')
    def root():
        return send_from_directory(app.static_folder, 'index.html')

    return app 