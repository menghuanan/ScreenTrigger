use std::collections::VecDeque;
use std::fs::File;
use std::io::Write;
use std::sync::{Arc, Condvar, Mutex};

#[derive(Default)]
struct Queues {
    ordinary: VecDeque<Vec<u8>>,
    priority: VecDeque<Vec<u8>>,
    closed: bool,
}

/// The pipe can block indefinitely. Its worker never holds the supervisor or router lock.
pub(super) struct SessionWriter(Arc<(Mutex<Queues>, Condvar)>);

impl SessionWriter {
    pub(super) fn new(mut pipe: File, failed: impl FnOnce() + Send + 'static) -> Self {
        let state = Arc::new((Mutex::new(Queues::default()), Condvar::new()));
        let worker = Arc::clone(&state);
        std::thread::spawn(move || loop {
            let frame = {
                let (lock, wake) = &*worker;
                let queue = lock.lock().unwrap_or_else(|p| p.into_inner());
                let mut queue = wake.wait_while(queue, |q| !q.closed && q.priority.is_empty() && q.ordinary.is_empty())
                    .unwrap_or_else(|p| p.into_inner());
                if queue.closed { return; }
                queue.priority.pop_front().or_else(|| queue.ordinary.pop_front()).expect("writer notified with a frame")
            };
            if pipe.write_all(&frame).is_err() {
                worker.0.lock().unwrap_or_else(|p| p.into_inner()).closed = true;
                failed();
                return;
            }
        });
        Self(state)
    }

    pub(super) fn send(&self, frame: Vec<u8>, priority: bool) -> Result<(), String> {
        let (lock, wake) = &*self.0;
        let mut queues = lock.lock().unwrap_or_else(|p| p.into_inner());
        if queues.closed { return Err("引擎输入管道已关闭".into()); }
        let (queue, limit) = if priority { (&mut queues.priority, 32) } else { (&mut queues.ordinary, 64) };
        if queue.len() >= limit { return Err("引擎写入队列已满".into()); }
        queue.push_back(frame);
        wake.notify_one();
        Ok(())
    }
}

impl Drop for SessionWriter {
    fn drop(&mut self) {
        let (lock, wake) = &*self.0;
        let mut queue = lock.lock().unwrap_or_else(|p| p.into_inner());
        queue.closed = true;
        queue.ordinary.clear();
        queue.priority.clear();
        wake.notify_one();
    }
}
