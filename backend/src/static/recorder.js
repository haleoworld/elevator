/* Shared in-browser recorder. Used by /coaching and /practice/<qid>.
   Multiple panels per page can coexist via a unique key passed to each fn. */

(function () {
    const state = {};  // key -> { mediaRecorder, chunks, blob, mimeType, stream,
                       //          timerInterval, startedAt, uploadUrl, roundFieldId }

    function panelFor(key) {
        // Find the .record-controls wrapper that owns this key's buttons.
        // Supports multiple independent recorders on one page (e.g. the
        // interview-description recorder + the interview recorder).
        const trigger = document.querySelector(`[onclick*="'${key}'"]`);
        return trigger ? trigger.closest('.record-controls') : null;
    }
    function setState(key, name, msg) {
        const panel = panelFor(key);
        if (!panel) return;
        panel.querySelectorAll('[data-rec-state]').forEach(el => {
            el.hidden = el.dataset.recState !== name;
        });
        if (name === 'error' && msg) {
            const e = panel.querySelector('[data-rec-error]');
            if (e) e.textContent = msg;
        }
    }
    function pickMime() {
        if (typeof MediaRecorder === 'undefined') return null;
        const candidates = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4;codecs=mp4a.40.2',
            'audio/mp4',
            'audio/aac',
        ];
        return candidates.find(t => MediaRecorder.isTypeSupported(t)) || '';
    }
    function extForMime(m) {
        if (!m) return 'webm';
        if (m.includes('webm'))  return 'webm';
        if (m.includes('mp4'))   return 'm4a';
        if (m.includes('aac'))   return 'm4a';
        if (m.includes('wav'))   return 'wav';
        return 'webm';
    }
    function startTimer(key) {
        const panel = panelFor(key);
        const display = panel.querySelector('[data-rec-timer]');
        const s = state[key];
        s.timerInterval = setInterval(() => {
            const sec = Math.floor((Date.now() - s.startedAt) / 1000);
            const m = Math.floor(sec / 60), r = sec % 60;
            display.textContent = `${m}:${String(r).padStart(2, '0')}`;
        }, 250);
    }
    function stopTimer(key) {
        const s = state[key];
        if (s && s.timerInterval) clearInterval(s.timerInterval);
        if (s) s.timerInterval = null;
    }

    window.recStart = async function (key, uploadUrl, roundFieldId) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setState(key, 'error', 'This browser does not support microphone access.');
            return;
        }
        const mimeType = pickMime();
        if (mimeType === null) {
            setState(key, 'error', 'MediaRecorder is not supported in this browser.');
            return;
        }
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const opts = mimeType ? { mimeType } : undefined;
            const mr = new MediaRecorder(stream, opts);
            state[key] = {
                mediaRecorder: mr,
                chunks: [],
                blob: null,
                mimeType: mimeType || mr.mimeType || 'audio/webm',
                stream,
                startedAt: Date.now(),
                uploadUrl,
                roundFieldId,
            };
            mr.ondataavailable = e => {
                if (e.data && e.data.size > 0) state[key].chunks.push(e.data);
            };
            mr.onstop = () => {
                const s = state[key];
                s.blob = new Blob(s.chunks, { type: s.mimeType });
                s.stream.getTracks().forEach(t => t.stop());
                const audioEl = panelFor(key).querySelector('[data-rec-preview]');
                audioEl.src = URL.createObjectURL(s.blob);
                stopTimer(key);
                setState(key, 'recorded');
            };
            // Timeslice (1s) makes ondataavailable fire periodically — more
            // reliable on iOS Safari, which can otherwise drop the single
            // end-of-stream data event and produce an empty recording.
            mr.start(1000);
            setState(key, 'recording');
            startTimer(key);
        } catch (err) {
            setState(key, 'error', 'Could not start recording: ' + (err.message || err.name || err));
        }
    };

    // Expose the recorded blob + extension so callers can do custom uploads
    // (e.g. the interview-description transcribe+parse flow) instead of the
    // default redirecting upload.
    window.recGetBlob = function (key) {
        return (state[key] && state[key].blob) || null;
    };
    window.recGetExt = function (key) {
        return state[key] ? extForMime(state[key].mimeType) : 'webm';
    };

    window.recStop = function (key) {
        const s = state[key];
        if (s && s.mediaRecorder && s.mediaRecorder.state !== 'inactive') {
            s.mediaRecorder.stop();
        }
    };

    window.recReset = function (key) {
        const s = state[key];
        if (s) {
            try { s.stream && s.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
        }
        delete state[key];
        const panel = panelFor(key);
        const audio = panel && panel.querySelector('[data-rec-preview]');
        if (audio) audio.src = '';
        setState(key, 'idle');
    };

    window.recUpload = async function (key) {
        const s = state[key];
        if (!s || !s.blob) {
            alert('Nothing recorded yet.');
            return;
        }
        const panel = panelFor(key);
        const btn = panel.querySelector('[data-rec-upload-btn]');
        const orig = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Uploading…';
        try {
            const fd = new FormData();
            if (s.roundFieldId) {
                const r = document.getElementById(s.roundFieldId);
                if (r && r.value) fd.append('round', r.value);
            }
            // Interview wizard metadata — appended when present so the record
            // path carries the same company/date/round as the upload path.
            const wizCompany = document.getElementById('wiz-company');
            const wizDate = document.getElementById('wiz-date');
            const wizRound = document.getElementById('wiz-round');
            if (wizCompany && wizCompany.value) fd.append('company', wizCompany.value);
            if (wizDate && wizDate.value) fd.append('interview_date', wizDate.value);
            if (wizRound && wizRound.value && !s.roundFieldId) fd.append('round', wizRound.value);
            const ext = extForMime(s.mimeType);
            fd.append('file', s.blob, `recording-${Date.now()}.${ext}`);
            const res = await fetch(s.uploadUrl, { method: 'POST', body: fd, credentials: 'same-origin' });
            if (res.ok || res.redirected) {
                window.location.href = res.url;
            } else {
                throw new Error('Upload failed: HTTP ' + res.status);
            }
        } catch (err) {
            btn.disabled = false;
            btn.textContent = orig;
            setState(key, 'error', err.message || String(err));
        }
    };
})();
