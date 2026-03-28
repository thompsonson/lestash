use anyhow::{Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use futures_util::StreamExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::mpsc;
use tracing::{error, info, warn};
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

const WHISPER_MODEL: &str = "ggml-base.en.bin";
const SAMPLE_RATE: u32 = 16000;
const SILENCE_THRESHOLD_MS: u64 = 800;
const SILENCE_RMS: f32 = 0.01;
const MIN_AUDIO_SECS: f32 = 0.5;
const AUDIO_PREALLOC_SECS: usize = 5;
const MAX_BUFFER_SECS: usize = 30;
const POLL_INTERVAL_MS: u64 = 100;
const RMS_WINDOW_DIVISOR: usize = 10;

pub struct SttEngine {
    listening: Arc<AtomicBool>,
    stop_tx: tokio::sync::Mutex<Option<mpsc::Sender<()>>>,
}

impl SttEngine {
    pub fn new() -> Self {
        Self {
            listening: Arc::new(AtomicBool::new(false)),
            stop_tx: tokio::sync::Mutex::new(None),
        }
    }

    pub fn is_listening(&self) -> bool {
        self.listening.load(Ordering::Relaxed)
    }

    pub async fn start(&self, app: AppHandle) -> Result<()> {
        if self.is_listening() {
            return Ok(());
        }

        let path = model_path_for_app(&app);
        if !path.exists() {
            return Err(anyhow::anyhow!(
                "Whisper model not found at {}. Download ggml-base.en.bin to this location.",
                path.display()
            ));
        }

        let (stop_tx, stop_rx) = mpsc::channel::<()>(1);
        *self.stop_tx.lock().await = Some(stop_tx);
        self.listening.store(true, Ordering::Relaxed);

        let listening = self.listening.clone();

        tokio::task::spawn_blocking(move || {
            if let Err(e) = run_stt_loop(app, path, stop_rx, listening.clone()) {
                error!("STT loop error: {e}");
            }
            listening.store(false, Ordering::Relaxed);
        });

        Ok(())
    }

    pub async fn stop(&self) {
        self.listening.store(false, Ordering::Relaxed);
        if let Some(tx) = self.stop_tx.lock().await.take() {
            let _ = tx.send(()).await;
        }
    }
}

pub fn model_path() -> PathBuf {
    let data_dir = dirs_next::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("lestash");
    data_dir.join(WHISPER_MODEL)
}

/// Resolve the model path using Tauri's app data dir (works on Android).
/// Falls back to dirs_next for desktop.
pub fn model_path_for_app(app: &AppHandle) -> PathBuf {
    let data_dir = app
        .path()
        .app_data_dir()
        .unwrap_or_else(|_| {
            dirs_next::data_dir()
                .unwrap_or_else(|| PathBuf::from("."))
                .join("lestash")
        });
    data_dir.join(WHISPER_MODEL)
}

const MODEL_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";

pub async fn download_model(app: AppHandle) -> Result<String> {
    let dest = model_path_for_app(&app);
    if dest.exists() {
        info!("Model already exists at {}", dest.display());
        return Ok(dest.display().to_string());
    }

    // Ensure parent directory exists
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).context("Failed to create model directory")?;
    }

    info!("Downloading whisper model to {}", dest.display());

    let client = reqwest::Client::new();
    let resp = client
        .get(MODEL_URL)
        .send()
        .await
        .context("Failed to start model download")?;

    let total = resp.content_length().unwrap_or(0);
    let mut downloaded: u64 = 0;

    // Download to temp file, then rename
    let tmp_path = dest.with_extension("bin.tmp");
    let mut file =
        tokio::fs::File::create(&tmp_path)
            .await
            .context("Failed to create temp file")?;

    let mut stream = resp.bytes_stream();
    let mut last_emit = Instant::now();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.context("Download stream error")?;
        downloaded += chunk.len() as u64;

        tokio::io::AsyncWriteExt::write_all(&mut file, &chunk)
            .await
            .context("Failed to write chunk")?;

        // Emit progress at most every 200ms
        if last_emit.elapsed() > Duration::from_millis(200) {
            let _ = app.emit(
                "model-download-progress",
                serde_json::json!({ "downloaded": downloaded, "total": total }),
            );
            last_emit = Instant::now();
        }
    }

    // Final progress emit
    let _ = app.emit(
        "model-download-progress",
        serde_json::json!({ "downloaded": downloaded, "total": total }),
    );

    // Rename temp to final
    tokio::fs::rename(&tmp_path, &dest)
        .await
        .context("Failed to rename downloaded model")?;

    info!("Model download complete: {}", dest.display());
    Ok(dest.display().to_string())
}

fn run_stt_loop(
    app: AppHandle,
    model_path: PathBuf,
    mut stop_rx: mpsc::Receiver<()>,
    listening: Arc<AtomicBool>,
) -> Result<()> {
    info!("Loading whisper model from {}", model_path.display());
    let ctx = WhisperContext::new_with_params(
        model_path.to_str().unwrap(),
        WhisperContextParameters::default(),
    )
    .context("Failed to load whisper model")?;

    info!("Whisper model loaded, starting audio capture");
    let _ = app.emit("stt-status", "model_loaded");

    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .context("No input audio device found")?;

    if let Ok(desc) = device.description() {
        info!("Using input device: {}", desc.name());
    }

    let config = cpal::StreamConfig {
        channels: 1,
        sample_rate: SAMPLE_RATE,
        buffer_size: cpal::BufferSize::Default,
    };

    let audio_buf: Arc<std::sync::Mutex<Vec<f32>>> = Arc::new(std::sync::Mutex::new(
        Vec::with_capacity(SAMPLE_RATE as usize * AUDIO_PREALLOC_SECS),
    ));

    let buf_clone = audio_buf.clone();
    let stream = device.build_input_stream(
        &config,
        move |data: &[f32], _: &cpal::InputCallbackInfo| {
            if let Ok(mut buf) = buf_clone.lock() {
                buf.extend_from_slice(data);
            }
        },
        |err| {
            error!("Audio stream error: {err}");
        },
        None,
    )?;
    stream.play()?;

    let _ = app.emit("stt-status", "listening");

    let mut last_speech = Instant::now();
    let mut had_speech = false;

    while listening.load(Ordering::Relaxed) {
        if stop_rx.try_recv().is_ok() {
            break;
        }

        std::thread::sleep(Duration::from_millis(POLL_INTERVAL_MS));

        let audio_len = audio_buf.lock().unwrap().len();
        let audio_secs = audio_len as f32 / SAMPLE_RATE as f32;

        if audio_len == 0 {
            continue;
        }

        // Check RMS of recent audio for silence detection
        let recent_rms = {
            let buf = audio_buf.lock().unwrap();
            let recent_start = buf.len().saturating_sub(SAMPLE_RATE as usize / RMS_WINDOW_DIVISOR);
            let recent = &buf[recent_start..];
            if recent.is_empty() {
                0.0
            } else {
                (recent.iter().map(|s| s * s).sum::<f32>() / recent.len() as f32).sqrt()
            }
        };

        let is_silent = recent_rms < SILENCE_RMS;

        if !is_silent {
            last_speech = Instant::now();
            had_speech = true;
        }

        let silence_duration = last_speech.elapsed();
        let should_process = had_speech
            && is_silent
            && silence_duration > Duration::from_millis(SILENCE_THRESHOLD_MS)
            && audio_secs > MIN_AUDIO_SECS;

        if !should_process {
            // Cap buffer to prevent unbounded growth
            let max_samples = SAMPLE_RATE as usize * MAX_BUFFER_SECS;
            let mut buf = audio_buf.lock().unwrap();
            if buf.len() > max_samples {
                let drain_to = buf.len() - max_samples;
                buf.drain(..drain_to);
            }
            continue;
        }

        // Take audio and transcribe
        let samples: Vec<f32> = {
            let mut buf = audio_buf.lock().unwrap();
            let s = buf.clone();
            buf.clear();
            s
        };
        had_speech = false;

        info!(
            "Transcribing {:.1}s of audio",
            samples.len() as f32 / SAMPLE_RATE as f32
        );

        let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
        params.set_language(Some("en"));
        params.set_print_special(false);
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_timestamps(false);
        params.set_suppress_blank(true);
        params.set_suppress_nst(true);

        let mut state = ctx.create_state().context("Failed to create whisper state")?;

        if let Err(e) = state.full(params, &samples) {
            warn!("Whisper transcription failed: {e}");
            continue;
        }

        let num_segments = state.full_n_segments().unwrap_or(0);
        let mut text = String::new();
        for i in 0..num_segments {
            if let Ok(segment) = state.full_get_segment_text(i) {
                text.push_str(&segment);
            }
        }

        let text = text.trim().to_string();
        if text.is_empty() || text == "[BLANK_AUDIO]" {
            continue;
        }

        info!("Transcribed: {text}");
        let _ = app.emit("stt-transcription", &text);
    }

    drop(stream);
    info!("STT stopped");
    let _ = app.emit("stt-status", "stopped");
    Ok(())
}
