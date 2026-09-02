from .app import create_app

# Gunicorn imports this module. Model import/load remains deferred until
# authenticated readiness or the first authenticated transcription.
app = create_app()
