//! Windowed GameStream client: connects to host.py, decodes H.264 (openh264),
//! and presents frames with GPU-accelerated, aspect-preserving scaling (pixels
//! / wgpu) — the rendering path pygame couldn't do efficiently.
//!
//! Input forwarding and audio are not wired yet; this milestone verifies the
//! decode + GPU display path on real hardware.
//!
//! Usage: gamestream-client [host] [--port 9900] [--video-port 9901]

use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use pixels::{Pixels, SurfaceTexture};
use winit::dpi::LogicalSize;
use winit::event::{ElementState, Event, KeyboardInput, VirtualKeyCode, WindowEvent};
use winit::event_loop::{ControlFlow, EventLoop};
use winit::window::WindowBuilder;

use gamestream_client::crypto::{hex_decode, SessionCipher};
use gamestream_client::net::connect;
use gamestream_client::video::{spawn_receiver, FrameSink};

fn main() {
    let mut host = "127.0.0.1".to_string();
    let mut port: u16 = 9900;
    let mut video_port: u16 = 9901;
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--port" if i + 1 < args.len() => { port = args[i + 1].parse().unwrap_or(port); i += 2; }
            "--video-port" if i + 1 < args.len() => { video_port = args[i + 1].parse().unwrap_or(video_port); i += 2; }
            s if !s.starts_with("--") => { host = s.to_string(); i += 1; }
            _ => i += 1,
        }
    }

    let ctrl = connect(&host, port, video_port).expect("connect/handshake");
    let cfg = &ctrl.config;
    let bw = cfg.get("width").and_then(|v| v.as_i64()).unwrap_or(1920) as u32;
    let bh = cfg.get("height").and_then(|v| v.as_i64()).unwrap_or(1080) as u32;
    let codec = cfg.get("codec").and_then(|v| v.as_str()).unwrap_or("?").to_string();
    let encrypted = cfg.get("encrypted").and_then(|v| v.as_bool()).unwrap_or(false);
    println!("connected: {bw}x{bh} codec={codec} encrypted={encrypted}");
    println!("(Esc or close the window to quit)");

    let cipher = if encrypted {
        cfg.get("session_key").and_then(|v| v.as_str()).and_then(hex_decode).and_then(|k| SessionCipher::new(&k))
    } else {
        None
    };

    let running = Arc::new(AtomicBool::new(true));
    let sink: FrameSink = Arc::new(Mutex::new(None));
    spawn_receiver(video_port, cipher, running, Some(sink.clone())).expect("bind video");

    // Keep the control connection open so the host keeps streaming.
    let _keep = ctrl.stream;

    let event_loop = EventLoop::new();
    let init = LogicalSize::new(1280.0, 1280.0 * bh as f64 / bw as f64);
    let window = WindowBuilder::new()
        .with_title(format!("GameStream — {host}"))
        .with_inner_size(init)
        .build(&event_loop)
        .expect("window");

    let mut pixels = {
        let sz = window.inner_size();
        let st = SurfaceTexture::new(sz.width.max(1), sz.height.max(1), &window);
        Pixels::new(bw, bh, st).expect("pixels")
    };

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Poll;
        match event {
            Event::WindowEvent { event, .. } => match event {
                WindowEvent::CloseRequested
                | WindowEvent::KeyboardInput {
                    input: KeyboardInput {
                        state: ElementState::Pressed,
                        virtual_keycode: Some(VirtualKeyCode::Escape),
                        ..
                    },
                    ..
                } => *control_flow = ControlFlow::Exit,
                WindowEvent::Resized(sz) => {
                    if pixels.resize_surface(sz.width.max(1), sz.height.max(1)).is_err() {
                        *control_flow = ControlFlow::Exit;
                    }
                }
                _ => {}
            },
            Event::RedrawRequested(_) => {
                if let Some(fb) = sink.lock().unwrap().as_ref() {
                    if fb.w as u32 == bw && fb.h as u32 == bh {
                        pixels.frame_mut().copy_from_slice(&fb.rgba);
                    }
                }
                if pixels.render().is_err() {
                    *control_flow = ControlFlow::Exit;
                }
            }
            Event::MainEventsCleared => window.request_redraw(),
            _ => {}
        }
    });
}
