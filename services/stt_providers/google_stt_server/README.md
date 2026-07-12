# Google STT Server

This service is responsible for capturing audio from the system's microphone, performing real-time speech-to-text transcription using the Google Cloud Speech-to-Text API, and forwarding the resulting transcripts to the `wheelhouse` service via a WebSocket connection.
