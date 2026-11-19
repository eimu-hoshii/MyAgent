/**
 * AIエージェント 管制コンソール & 自律型VTuber 配信スタジオ (NIA v3.0) メインコントローラー。
 * - WebSocket によるリアルタイム状態同期
 * - デスクトップ常駐マスコット起動制御・画面認識(Vision)・クリップボード/ファイルD&D解析
 * - ポモドーロタイマー・アクティブウィンドウ表示・ホットキー(Alt+Space)・ウェイクワード対応
 * - Web Audio API による音声波形リップシンク・効果音(SE)シンセ・配信BGM＆オートダッキング
 * - 全5タブ統合設定モーダル・長期記憶CRUD・予定表＆スヌーズ管理
 */

import { AvatarRenderer } from "./avatar.js?v=14";

const EMOTION_LABELS = {
    normal: "通常 (NORMAL)",
    smile: "微笑み (SMILE)",
    joy: "喜び (JOY)",
    serious: "真剣・怒り (SERIOUS)",
    surprised: "驚き (SURPRISED)",
    troubled: "困り顔 (TROUBLED)",
};

class AgentController {
    constructor() {
        const params = new URLSearchParams(window.location.search);
        this.isObsMode = params.get("mode") === "obs";
        this.isMascotMode = params.get("mode") === "mascot";
        if (this.isObsMode) {
            document.body.classList.add("obs-mode");
        } else if (this.isMascotMode) {
            document.body.classList.add("mascot-mode");
        }

        const canvas = document.getElementById("avatar-canvas");
        this.avatar = new AvatarRenderer(canvas, this.isObsMode);
        if (this.isMascotMode) {
            this.avatar.setViewMode("full");
        }
        this.availableOutfits = [];
        this.availableAccessories = [];
        this.avatar.onInteract = (kind) => this._sendAvatarInteraction(kind);

        this.ws = null;
        this.audioCtx = null;
        this.currentAudio = null;
        this.audioMonitorFrame = null;
        this.emotionResetTimer = null;
        this.subtitleHideTimer = null;
        this.subtitleClearTextTimer = null;
        this.pipWindow = null;
        this.mascotPopup = null;
        this.selectedAudioSinkId = localStorage.getItem("nia_audio_sink_id") || "";
        this.recognition = null;
        this.isListeningMic = false;

        // 配信用アンビエントBGMノード＆歌枠再生オーディオ
        this.bgmNodes = null;
        this.bgmMasterGain = null;
        this.isBgmPlaying = false;
        this.karaokeAudio = null;
        this.karaokeTimer = null;
        this.newsOverlayTimer = null;
        this.mailbagOverlayTimer = null;
        this.pendingApprovals = [];
        this.speechStreamQueue = null;
        this.availablePresets = [];

        this.currentConfig = {
            agent_name: "ニア (NIA)",
            mode: "agent",
            ollama_model: "huihui_ai/gemma-4-abliterated:12b",
            ollama_model_vtuber: "gemma4:12b",
            active_ollama_model: "huihui_ai/gemma-4-abliterated:12b",
            tts_provider: "edge_tts",
            proactive_enabled: true,
            discord_notify_proactive: true,
            discord_notify_autonomous: false,
            hotkey_ptt: "Alt+Space",
            wake_word: "ニア",
            wake_word_required: false,
            obs_se_enabled: true,
            obs_bgm_enabled: false,
            obs_bgm_volume: 0.18,
            obs_bgm_ducking_ratio: 0.25,
            obs_motion_intensity: 1.0,
            obs_subtitle_scale: 1.0,
            schedule_snooze_min: 5,
            conversation_history_limit: 12,
            ui_view_level: "expert",
            active_preset_id: "",
            llm_routing_mode: "auto",
            ollama_model_fast: "gemma4:e4b",
            ollama_model_smart: "gemma4:26b",
            llm_self_review_enabled: true,
            streaming_tts_enabled: true,
            semantic_memory_enabled: true,
            memory_auto_consolidate: true,
            bilingual_enabled: false,
            bilingual_target_lang: "en",
        };

        this._bindUIEvents();
        this._bindDragAndDrop();
        this._bindGlobalHotkeys();
        this._initSpeechRecognition();
        this._initAudioSyncChannel();
        this._connectWebSocket();
    }

    /**
     * 複数ウィンドウ（メイン画面・PiPマスコット・OBS画面）同時起動時の二重音声再生（エコー現象）を防ぎ、
     * メイン画面が音声再生を担当して他のウィンドウへ口パク波形のみを同期する。
     */
    _initAudioSyncChannel() {
        this.audioSyncChannel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("nia_audio_sync") : null;
        this.isMainConsole = !this.isObsMode && !this.isMascotMode;

        if (this.isMainConsole) {
            const updateOwnerHeartbeat = () => {
                try {
                    localStorage.setItem("nia_main_audio_owner_ts", String(Date.now()));
                } catch (_) {}
            };
            updateOwnerHeartbeat();
            this._ownerHeartbeatTimer = setInterval(updateOwnerHeartbeat, 2000);
            window.addEventListener("beforeunload", () => {
                clearInterval(this._ownerHeartbeatTimer);
                try {
                    localStorage.removeItem("nia_main_audio_owner_ts");
                } catch (_) {}
            });
        } else if (this.audioSyncChannel) {
            this.audioSyncChannel.onmessage = (e) => {
                const msg = e.data;
                if (!msg) return;
                if (msg.type === "lipsync_start") {
                    this._clearEmotionReset();
                    this._clearSubtitleTimer();
                    this.avatar.startSpeech(msg.speechText || "", "webaudio");
                } else if (msg.type === "lipsync_level") {
                    this.avatar.setAudioLevel(Number(msg.level) || 0);
                } else if (msg.type === "lipsync_stop") {
                    this.avatar.stopSpeech();
                    this._scheduleEmotionReset(2500);
                    this._scheduleSubtitleClear(5000);
                }
            };
        }
    }

    _shouldSkipAudioForDuplicatePrevention() {
        if (this.isMainConsole) return false;
        try {
            const ts = Number(localStorage.getItem("nia_main_audio_owner_ts") || 0);
            // メインコンソールが直近4.5秒以内にアクティブなら、マスコット/OBS側での二重再生（エコー）を抑制する
            return ts > 0 && (Date.now() - ts) < 4500;
        } catch (_) {
            return false;
        }
    }

    /**
     * AudioContext を確実に初期化・再開する。
     */
    async _ensureAudioContext() {
        if (!this.audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            this.audioCtx = new AudioContextClass();
        }
        if (this.audioCtx.state === "suspended") {
            await this.audioCtx.resume();
        }
        return this.audioCtx;
    }

    /**
     * スパチャや予定アラート時の効果音(SE)をWeb Audioシンセで鳴らす。
     * @param {"superchat"|"alert"} kind
     */
    async _playSE(kind = "alert") {
        if (!this.currentConfig.obs_se_enabled) return;
        try {
            const ctx = await this._ensureAudioContext();
            const now = ctx.currentTime;
            const notes =
                kind === "superchat"
                    ? [523.25, 659.25, 783.99, 1046.5] // C5-E5-G5-C6 ファンファーレ
                    : [880.0, 1318.5]; // A5-E6 チャイム

            notes.forEach((freq, idx) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = kind === "superchat" ? "triangle" : "sine";
                osc.frequency.setValueAtTime(freq, now + idx * 0.09);

                gain.gain.setValueAtTime(0.001, now + idx * 0.09);
                gain.gain.exponentialRampToValueAtTime(0.18, now + idx * 0.09 + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.09 + 0.42);

                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(now + idx * 0.09);
                osc.stop(now + idx * 0.09 + 0.45);
            });
        } catch (err) {
            console.warn("SE再生エラー:", err);
        }
    }

    /**
     * 配信用アンビエントBGMを開始・停止する（発話時オートダッキング対応）。
     */
    async toggleBgm(forceState) {
        const target = typeof forceState === "boolean" ? forceState : !this.isBgmPlaying;
        if (!target) {
            if (this.bgmNodes) {
                this.bgmNodes.forEach((n) => {
                    try {
                        n.stop();
                    } catch (_) {}
                });
                this.bgmNodes = null;
            }
            this.bgmMasterGain = null;
            this.isBgmPlaying = false;
            this._updateBgmButtonUI();
            return;
        }

        try {
            const ctx = await this._ensureAudioContext();
            if (this.bgmNodes) return;

            const masterGain = ctx.createGain();
            masterGain.gain.setValueAtTime(this.currentConfig.obs_bgm_volume || 0.18, ctx.currentTime);
            masterGain.connect(ctx.destination);
            this.bgmMasterGain = masterGain;

            // シーン・雰囲気別コードプリセット (ambient | lofi_chill | news_pulse | tense_synth | ending_warm)
            const scene = (this.currentConfig && this.currentConfig.obs_bgm_scene) || "ambient";
            const presets = {
                ambient: { freqs: [130.81, 196.0, 246.94, 293.66], cutoff: 420 },
                lofi_chill: { freqs: [110.0, 164.81, 196.0, 246.94], cutoff: 340 },
                news_pulse: { freqs: [146.83, 220.0, 293.66, 329.63], cutoff: 620 },
                tense_synth: { freqs: [98.0, 146.83, 155.56, 233.08], cutoff: 520 },
                ending_warm: { freqs: [130.81, 164.81, 196.0, 246.94], cutoff: 390 },
            };
            const preset = presets[scene] || presets.ambient;
            const freqs = preset.freqs;
            const nodes = [];
            freqs.forEach((f, i) => {
                const osc = ctx.createOscillator();
                const filter = ctx.createBiquadFilter();
                const subGain = ctx.createGain();

                osc.type = scene === "tense_synth" ? "sawtooth" : (i % 2 === 0 ? "sine" : "triangle");
                osc.frequency.setValueAtTime(f, ctx.currentTime);

                filter.type = "lowpass";
                filter.frequency.setValueAtTime(preset.cutoff, ctx.currentTime);

                subGain.gain.setValueAtTime((scene === "tense_synth" ? 0.08 : 0.12) / freqs.length, ctx.currentTime);

                osc.connect(filter);
                filter.connect(subGain);
                subGain.connect(masterGain);
                osc.start();
                nodes.push(osc);
            });

            this.bgmNodes = nodes;
            this.isBgmPlaying = true;
            this._updateBgmButtonUI();
        } catch (err) {
            console.warn("BGM起動エラー:", err);
        }
    }

    /**
     * ニアの発話開始・終了に合わせてBGM音量を自動減衰（オートダッキング）させる。
     * @param {boolean} isSpeaking
     */
    _setBgmDucking(isSpeaking) {
        if (!this.bgmMasterGain || !this.audioCtx) return;
        const baseVol = Number(this.currentConfig.obs_bgm_volume) || 0.18;
        const duckRatio = Number(this.currentConfig.obs_bgm_ducking_ratio) || 0.25;
        const targetVol = isSpeaking ? baseVol * duckRatio : baseVol;
        this.bgmMasterGain.gain.setTargetAtTime(targetVol, this.audioCtx.currentTime, 0.12);
    }

    _updateBgmButtonUI() {
        const btn = document.getElementById("btn-toggle-bgm");
        if (!btn) return;
        btn.classList.toggle("active", this.isBgmPlaying);
        btn.textContent = this.isBgmPlaying ? "🎵 配信BGM: ON (自動減衰)" : "🎵 配信BGM: OFF";
    }

    /**
     * UI上のボタン・フォーム・モーダルイベントをバインドする。
     */
    _bindUIEvents() {
        // モード切替ボタン (AIエージェント ⇄ VTuber)
        document.querySelectorAll(".mode-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                const targetMode = btn.dataset.mode;
                this.updateConfig({ mode: targetMode });
            });
        });

        // デスクトップ常駐マスコット起動/停止
        // サーバーがユーザーデスクトップ(Default)上で稼働時はWindowsネイティブ透過マスコットを起動し、
        // AI開発環境の隔離コンソール(exebox)上で稼働時はブラウザのDocument Picture-in-Picture（最前面・URLバーなし）を起動する
        const mascotBtn = document.getElementById("btn-toggle-mascot");
        if (mascotBtn) {
            mascotBtn.addEventListener("click", async () => {
                if (this.currentConfig.native_desktop_available === false) {
                    await this._toggleBrowserPiPMascot();
                    return;
                }
                await fetch("/api/mascot/toggle", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
            });
        }

        // マスコットモード専用ミニツールバー（サイズ変更・上下移動・全身切替・ダブルクリック会話欄開閉）
        const bindClick = (id, fn) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener("click", fn);
        };
        bindClick("btn-mascot-zoom-in", () => this.avatar.adjustZoom(0.14));
        bindClick("btn-mascot-zoom-out", () => this.avatar.adjustZoom(-0.14));
        bindClick("btn-mascot-pan-up", () => this.avatar.adjustPan(0, -45));
        bindClick("btn-mascot-pan-down", () => this.avatar.adjustPan(0, 45));
        bindClick("btn-mascot-reset-pos", () => this.avatar.resetTransform());

        bindClick("btn-stage-zoom-in", () => this.avatar.adjustZoom(0.14));
        bindClick("btn-stage-zoom-out", () => this.avatar.adjustZoom(-0.14));
        bindClick("btn-stage-reset-pos", () => this.avatar.resetTransform());

        const mascotViewBtn = document.getElementById("btn-mascot-view");
        if (mascotViewBtn) {
            mascotViewBtn.addEventListener("click", () => {
                const nextMode = this.avatar.viewMode === "bust" ? "full" : "bust";
                this.avatar.setViewMode(nextMode);
            });
        }
        const mascotChatBtn = document.getElementById("btn-mascot-chat");
        if (mascotChatBtn) {
            mascotChatBtn.addEventListener("click", () => {
                document.body.classList.toggle("show-mascot-input");
                const chatInput = document.getElementById("chat-input");
                if (document.body.classList.contains("show-mascot-input") && chatInput) {
                    chatInput.focus();
                }
            });
        }
        const mascotVisionBtn = document.getElementById("btn-mascot-vision");
        if (mascotVisionBtn) {
            mascotVisionBtn.addEventListener("click", () => this.triggerVisionAnalyze());
        }
        const avatarCanvas = document.getElementById("avatar-canvas");
        if (avatarCanvas && this.isMascotMode) {
            avatarCanvas.addEventListener("dblclick", () => {
                document.body.classList.toggle("show-mascot-input");
            });
        }

        // 画面認識 (Vision) ボタン
        const visionBtn = document.getElementById("btn-vision-capture");
        if (visionBtn) {
            visionBtn.addEventListener("click", () => this.triggerVisionAnalyze());
        }

        // クリップボード解析ボタン
        const clipBtn = document.getElementById("btn-clipboard-analyze");
        if (clipBtn) {
            clipBtn.addEventListener("click", async () => {
                const res = await fetch("/api/clipboard_analyze", { method: "POST" }).then((r) => r.json());
                if (!res.ok) {
                    this._appendThoughtItem(`[クリップボード] ${res.error}`, "tool");
                }
            });
        }

        // ポモドーロ操作ボタン
        const pomoToggle = document.getElementById("btn-pomo-toggle");
        if (pomoToggle) {
            pomoToggle.addEventListener("click", () => {
                fetch("/api/pomodoro", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "toggle" }),
                });
            });
        }
        const pomoSwitch = document.getElementById("btn-pomo-switch");
        if (pomoSwitch) {
            pomoSwitch.addEventListener("click", () => {
                fetch("/api/pomodoro", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "switch_phase" }),
                });
            });
        }
        const pomoReset = document.getElementById("btn-pomo-reset");
        if (pomoReset) {
            pomoReset.addEventListener("click", () => {
                fetch("/api/pomodoro", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "reset" }),
                });
            });
        }

        // 統合設定モーダル開閉＆タブ切替
        const personaModal = document.getElementById("persona-modal");
        const openPersonaBtn = document.getElementById("btn-open-persona");
        const closePersonaBtn = document.getElementById("btn-close-persona");
        const cancelPersonaBtn = document.getElementById("btn-cancel-persona");
        const savePersonaBtn = document.getElementById("btn-save-persona");
        const resetPersonaBtn = document.getElementById("btn-reset-persona");

        document.querySelectorAll(".modal-tab").forEach((tabBtn) => {
            tabBtn.addEventListener("click", () => {
                document.querySelectorAll(".modal-tab").forEach((b) => b.classList.remove("active"));
                document.querySelectorAll(".tab-pane").forEach((p) => {
                    p.classList.add("hidden");
                    p.classList.remove("active");
                });
                tabBtn.classList.add("active");
                const pane = document.getElementById(tabBtn.dataset.tab);
                if (pane) {
                    pane.classList.remove("hidden");
                    pane.classList.add("active");
                }
            });
        });

        // スライダーのリアルタイム数値ラベル更新
        const rangeBindings = [
            ["cfg-tts-vol", "val-tts-vol"],
            ["cfg-tts-speed", "val-tts-speed"],
            ["cfg-tts-pitch", "val-tts-pitch"],
            ["cfg-tts-into", "val-tts-into"],
            ["cfg-obs-motion", "val-obs-motion"],
            ["cfg-obs-sub", "val-obs-sub"],
            ["cfg-bgm-vol", "val-bgm-vol"],
            ["cfg-bgm-duck", "val-bgm-duck"],
        ];
        rangeBindings.forEach(([inputId, labelId]) => {
            const el = document.getElementById(inputId);
            const lbl = document.getElementById(labelId);
            if (el && lbl) {
                el.addEventListener("input", () => {
                    lbl.textContent = el.value;
                });
            }
        });

        if (openPersonaBtn && personaModal) {
            openPersonaBtn.addEventListener("click", () => {
                this._populatePersonaModal();
                this._refreshStartupStatus();
                this._populateAudioSinks();
                fetch("/api/engines/status").then((r) => r.json()).then((d) => {
                    if (d && d.engine_status) this._renderEngineStatus(d.engine_status);
                }).catch(() => {});
                personaModal.classList.remove("hidden");
            });
        }

        // 発話即時停止 / 音声ミュート / エンジン稼働ランプ
        const stopSpeechBtn = document.getElementById("btn-stop-speech");
        if (stopSpeechBtn) {
            stopSpeechBtn.addEventListener("click", () => {
                this.stopCurrentSpeech();
                fetch("/api/speech/stop", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ clear_queue: false }),
                });
            });
        }
        const muteBtn = document.getElementById("btn-toggle-mute");
        if (muteBtn) {
            muteBtn.addEventListener("click", () => {
                fetch("/api/voice/mute", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
            });
        }
        const enginePill = document.getElementById("engine-status-pill");
        if (enginePill) {
            enginePill.addEventListener("click", async () => {
                enginePill.textContent = "🔄 確認中...";
                await fetch("/api/engines/status");
            });
        }
        const startAivisBtn = document.getElementById("btn-start-aivis");
        if (startAivisBtn) {
            startAivisBtn.addEventListener("click", async () => {
                startAivisBtn.disabled = true;
                startAivisBtn.textContent = "⏳ Aivis確認中...";
                try {
                    const res = await fetch("/api/engines/aivisspeech/start", { method: "POST" }).then((r) =>
                        r.json()
                    );
                    if (res && res.engine_status) {
                        this._renderEngineStatus(res.engine_status);
                    }
                    if (res && res.message) {
                        this._appendThoughtItem(`[AivisSpeech] ${res.message}`, "tool");
                    } else if (res && res.error) {
                        this._appendThoughtItem(`[AivisSpeechエラー] ${res.error}`, "tool");
                        alert(res.error);
                    }
                } catch (e) {
                    console.warn("AivisSpeech 裏起動エラー:", e);
                } finally {
                    startAivisBtn.disabled = false;
                }
            });
        }

        // スキルプリセットパック適用・シンプル/フルUI切替・バイリンガル字幕トグル
        const presetSelect = document.getElementById("select-skill-preset");
        if (presetSelect) {
            presetSelect.addEventListener("change", async () => {
                const presetId = presetSelect.value;
                if (!presetId) return;
                try {
                    const res = await fetch("/api/presets/apply", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ preset_id: presetId }),
                    }).then((r) => r.json());
                    if (res && res.ok) {
                        if (res.config) this._applyConfigState(res.config);
                        if (res.memories) this._renderMemories(res.memories);
                        if (Array.isArray(res.history)) {
                            this._renderInitialHistory(res.history, { showSubtitle: false, appendThoughts: false });
                        }
                        this._appendThoughtItem(
                            `[プリセット適用] 「${res.preset?.name || presetId}」を設定しました。`,
                            "tool"
                        );
                    }
                } catch (e) {
                    console.warn("プリセット適用失敗:", e);
                }
            });
        }
        const uiLevelBtn = document.getElementById("btn-toggle-ui-level");
        if (uiLevelBtn) {
            uiLevelBtn.addEventListener("click", () => {
                const nextLevel = this.currentConfig.ui_view_level === "simple" ? "expert" : "simple";
                this.updateConfig({ ui_view_level: nextLevel });
            });
        }
        const bilingualBtn = document.getElementById("btn-toggle-bilingual");
        if (bilingualBtn) {
            bilingualBtn.addEventListener("click", () => {
                const nextEnabled = !this.currentConfig.bilingual_enabled;
                this.updateConfig({ bilingual_enabled: nextEnabled });
            });
        }
        const pointerModeBtn = document.getElementById("btn-pointer-mode");
        if (pointerModeBtn) {
            pointerModeBtn.addEventListener("click", () => {
                const nextMode = this.avatar.pointerMode === "interact" ? "pan" : "interact";
                this.avatar.setPointerMode(nextMode);
                pointerModeBtn.classList.toggle("active", nextMode === "interact");
                pointerModeBtn.textContent = nextMode === "interact" ? "✋ ふれあいON" : "✥ 位置移動モード";
            });
        }
        const affectionResetBtn = document.getElementById("btn-affection-reset");
        if (affectionResetBtn) {
            affectionResetBtn.addEventListener("click", async () => {
                try {
                    await fetch("/api/affection/reset", { method: "POST" });
                } catch (_) {}
            });
        }
        const genreSel = document.getElementById("select-outfit-genre");
        const outfitSel = document.getElementById("select-outfit-item");
        if (genreSel && outfitSel) {
            genreSel.addEventListener("change", () => {
                this.selectedOutfitGenre = genreSel.value;
                this._populateOutfitSelect(this.selectedOutfitGenre, this.currentConfig.active_outfit || "default");
                // ジャンル切替時、先頭の衣装へ即座に着替えて快適に試せるようにする
                if (outfitSel.value && outfitSel.value !== (this.currentConfig.active_outfit || "default")) {
                    fetch("/api/appearance", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ outfit: outfitSel.value }),
                    }).catch(() => {});
                }
            });
            outfitSel.addEventListener("change", () => {
                const oid = outfitSel.value;
                if (!oid) return;
                fetch("/api/appearance", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ outfit: oid }),
                }).catch(() => {});
            });
        }
        this._setupOutfitUploadModal();

        // OBS配信テーマ更新 & 常連リスナー記憶全消去
        const setTopicBtn = document.getElementById("btn-set-topic");
        const topicInput = document.getElementById("input-stream-topic");
        if (setTopicBtn && topicInput) {
            setTopicBtn.addEventListener("click", () => {
                fetch("/api/topic", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ topic: topicInput.value.trim() }),
                });
            });
        }
        const clearViewersBtn = document.getElementById("btn-clear-viewers");
        if (clearViewersBtn) {
            clearViewersBtn.addEventListener("click", () => {
                if (confirm("記録された常連リスナー記憶をすべて消去しますか？")) {
                    fetch("/api/viewers", { method: "DELETE" });
                }
            });
        }

        // スタートアップ登録・オーディオ出力先・キャッシュ掃除
        const startupBtn = document.getElementById("btn-toggle-startup");
        if (startupBtn) {
            startupBtn.addEventListener("click", async () => {
                const res = await fetch("/api/startup/toggle", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                }).then((r) => r.json());
                this._renderStartupStatus(res);
            });
        }
        const refreshSinkBtn = document.getElementById("btn-refresh-audio-sinks");
        if (refreshSinkBtn) {
            refreshSinkBtn.addEventListener("click", () => this._populateAudioSinks());
        }
        const sinkSelect = document.getElementById("cfg-audio-sink");
        if (sinkSelect) {
            sinkSelect.addEventListener("change", () => {
                this.selectedAudioSinkId = sinkSelect.value || "";
                localStorage.setItem("nia_audio_sink_id", this.selectedAudioSinkId);
            });
        }
        const cleanupCacheBtn = document.getElementById("btn-cleanup-cache");
        if (cleanupCacheBtn) {
            cleanupCacheBtn.addEventListener("click", async () => {
                const res = await fetch("/api/audio/cleanup", { method: "POST" }).then((r) => r.json());
                if (res && res.engine_status) this._renderEngineStatus(res.engine_status);
                this._appendThoughtItem(`[音声キャッシュ整理] ${res.removed || 0}件の古い音声ファイルを削除しました。`, "tool");
            });
        }
        const testDiscordBtn = document.getElementById("btn-test-discord");
        if (testDiscordBtn) {
            testDiscordBtn.addEventListener("click", async () => {
                testDiscordBtn.disabled = true;
                try {
                    const res = await fetch("/api/discord/test", { method: "POST" }).then((r) => r.json());
                    if (res.discord_status) this._renderDiscordStatus(res.discord_status);
                    if (res.ok) {
                        this._appendThoughtItem(`[Discord] テストDMを「${res.user}」へ送信しました。`, "tool");
                    } else {
                        this._appendThoughtItem(`[Discordエラー] ${res.error || "送信に失敗しました"}`, "tool");
                        alert(res.error || "Discordテスト送信に失敗しました。");
                    }
                } finally {
                    testDiscordBtn.disabled = false;
                }
            });
        }
        const clearDiscordTokenBtn = document.getElementById("btn-clear-discord-token");
        if (clearDiscordTokenBtn) {
            clearDiscordTokenBtn.addEventListener("click", async () => {
                if (!confirm("保存済みの Discord Bot トークンを消去して接続を停止しますか？")) return;
                const res = await this.updateConfig({ clear_discord_token: true, discord_enabled: false });
                const tokEl = document.getElementById("cfg-discord-token");
                if (tokEl) tokEl.value = "";
                if (res && res.config) {
                    this.currentConfig = res.config;
                    this._populatePersonaModal();
                }
            });
        }
        const closeModal = () => {
            if (personaModal) personaModal.classList.add("hidden");
        };
        if (closePersonaBtn) closePersonaBtn.addEventListener("click", closeModal);
        if (cancelPersonaBtn) cancelPersonaBtn.addEventListener("click", closeModal);
        if (personaModal) {
            personaModal.addEventListener("click", (e) => {
                if (e.target === personaModal) closeModal();
            });
        }

        if (savePersonaBtn) {
            savePersonaBtn.addEventListener("click", async () => {
                const payload = this._collectAllSettingsFromModal();
                await this.updateConfig(payload);
                closeModal();
            });
        }

        if (resetPersonaBtn) {
            resetPersonaBtn.addEventListener("click", async () => {
                const res = await this.updateConfig({ reset_persona: true });
                if (res && res.config) {
                    this.currentConfig = res.config;
                    this._populatePersonaModal();
                }
            });
        }

        // 思考・自律実行ログのクリアボタン
        const clearThoughtsBtn = document.getElementById("btn-clear-thoughts");
        if (clearThoughtsBtn) {
            clearThoughtsBtn.addEventListener("click", () => {
                const container = document.getElementById("thought-log");
                if (container) {
                    container.innerHTML = "";
                    this._appendThoughtItem("思考・自律実行ログをクリアしました。", "thought");
                }
            });
        }

        // 対話・配信コメント履歴のクリアボタン
        const clearChatBtn = document.getElementById("btn-clear-chat");
        if (clearChatBtn) {
            clearChatBtn.addEventListener("click", async () => {
                const chatContainer = document.getElementById("chat-feed");
                if (chatContainer) chatContainer.innerHTML = "";
                await fetch("/api/conversations", { method: "DELETE" });
            });
        }

        // 長期記憶の手動追加・一括クリア
        const toggleAddMemBtn = document.getElementById("btn-toggle-add-mem");
        const addMemBox = document.getElementById("form-add-memory");
        if (toggleAddMemBtn && addMemBox) {
            toggleAddMemBtn.addEventListener("click", () => {
                addMemBox.classList.toggle("hidden");
            });
        }
        const submitMemBtn = document.getElementById("btn-submit-mem");
        if (submitMemBtn) {
            submitMemBtn.addEventListener("click", async () => {
                const cat = document.getElementById("new-mem-cat").value;
                const imp = Number(document.getElementById("new-mem-imp").value) || 4;
                const input = document.getElementById("new-mem-content");
                const content = input.value.trim();
                if (!content) return;
                await fetch("/api/memories", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ category: cat, content, importance: imp }),
                });
                input.value = "";
                if (addMemBox) addMemBox.classList.add("hidden");
            });
        }
        const clearMemoriesBtn = document.getElementById("btn-clear-memories");
        if (clearMemoriesBtn) {
            clearMemoriesBtn.addEventListener("click", async () => {
                const semInput = document.getElementById("input-mem-semantic");
                if (semInput) semInput.value = "";
                await fetch("/api/memories", { method: "DELETE" });
            });
        }
        const consolidateMemBtn = document.getElementById("btn-consolidate-mem");
        if (consolidateMemBtn) {
            consolidateMemBtn.addEventListener("click", async () => {
                consolidateMemBtn.disabled = true;
                const oldLabel = consolidateMemBtn.textContent;
                consolidateMemBtn.textContent = "⏳整理中";
                try {
                    const res = await fetch("/api/memories/consolidate", { method: "POST" }).then((r) => r.json());
                    if (res && res.ok) {
                        if (res.memories) this._renderMemories(res.memories);
                        this._appendThoughtItem(
                            `[記憶統合] 類似統合 ${res.merged_count || 0}件 / 整理 ${res.pruned_count || 0}件（${res.before_count || 0} → ${res.after_count || 0}件）`,
                            "tool"
                        );
                    }
                } catch (e) {
                    console.warn("記憶統合エラー:", e);
                } finally {
                    consolidateMemBtn.disabled = false;
                    consolidateMemBtn.textContent = oldLabel;
                }
            });
        }
        const runSemanticSearch = async () => {
            const semInput = document.getElementById("input-mem-semantic");
            if (!semInput) return;
            const q = semInput.value.trim();
            if (!q) {
                try {
                    const res = await fetch("/api/memories").then((r) => r.json());
                    if (res && res.memories) this._renderMemories(res.memories);
                } catch (_) {}
                return;
            }
            try {
                const res = await fetch(`/api/memories/semantic?q=${encodeURIComponent(q)}&limit=12`).then((r) =>
                    r.json()
                );
                if (res && res.ok) {
                    this._renderMemories(res.items || []);
                }
            } catch (e) {
                console.warn("意味ベクトル検索失敗:", e);
            }
        };
        const memSemBtn = document.getElementById("btn-mem-semantic");
        const memSemInput = document.getElementById("input-mem-semantic");
        if (memSemBtn) {
            memSemBtn.addEventListener("click", runSemanticSearch);
        }
        if (memSemInput) {
            memSemInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    e.preventDefault();
                    runSemanticSearch();
                }
            });
            memSemInput.addEventListener("input", () => {
                if (!memSemInput.value.trim()) {
                    runSemanticSearch();
                }
            });
        }

        // 予定・監視タスクの手動追加
        const toggleAddTaskBtn = document.getElementById("btn-toggle-add-task");
        const addTaskBox = document.getElementById("form-add-task");
        if (toggleAddTaskBtn && addTaskBox) {
            toggleAddTaskBtn.addEventListener("click", () => {
                addTaskBox.classList.toggle("hidden");
            });
        }
        const submitTaskBtn = document.getElementById("btn-submit-task");
        if (submitTaskBtn) {
            submitTaskBtn.addEventListener("click", async () => {
                const titleInput = document.getElementById("new-task-title");
                const dueInput = document.getElementById("new-task-due");
                const repSelect = document.getElementById("new-task-repeat");
                const title = titleInput.value.trim();
                const dueRaw = dueInput.value.trim();
                if (!title) return;

                let due_minutes = 0;
                let due_iso = "";
                if (/^\d+(\.\d+)?$/.test(dueRaw)) {
                    due_minutes = parseFloat(dueRaw);
                } else if (dueRaw) {
                    due_iso = dueRaw;
                }

                await fetch("/api/tasks", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        title,
                        due_minutes,
                        due_iso,
                        repeat_rule: repSelect.value,
                    }),
                });
                titleInput.value = "";
                dueInput.value = "";
                if (addTaskBox) addTaskBox.classList.add("hidden");
            });
        }

        // 感情パレットボタン
        document.querySelectorAll(".emotion-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                this._clearEmotionReset();
                const emo = btn.dataset.emotion;
                this.avatar.setEmotion(emo);
                this._updateEmotionBadge(emo);
            });
        });

        // 目開け/目閉じトグルボタン
        const blinkBtn = document.getElementById("btn-toggle-blink");
        if (blinkBtn) {
            blinkBtn.addEventListener("click", () => {
                const forced = this.avatar.toggleForceBlink();
                blinkBtn.classList.toggle("active", forced);
                blinkBtn.textContent = forced ? "😌 目閉じ(固定中)" : "👁️ 目開け(自動瞬き)";
            });
        }

        // 口の形確認ボタン
        document.querySelectorAll(".vowel-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                document.querySelectorAll(".vowel-btn").forEach((b) => b.classList.remove("active"));
                btn.classList.add("active");
                const v = btn.dataset.vowel;
                this.avatar.setManualVowel(v === "auto" ? null : v);
            });
        });

        // 全身 / バストアップ切替
        const viewBtn = document.getElementById("btn-toggle-view");
        if (viewBtn) {
            viewBtn.addEventListener("click", () => {
                const nextMode = this.avatar.viewMode === "bust" ? "full" : "bust";
                this.avatar.setViewMode(nextMode);
                viewBtn.textContent = nextMode === "bust" ? "全身表示へ切替" : "バストアップへ切替";
            });
        }

        // 音声＆口パク即時テストボタン
        const testVoiceBtn = document.getElementById("btn-test-voice");
        if (testVoiceBtn) {
            testVoiceBtn.addEventListener("click", () => {
                this.triggerLocalVoiceDemo();
            });
        }

        // OBS透過ウィンドウを開くボタン
        const obsBtn = document.getElementById("btn-open-obs");
        if (obsBtn) {
            obsBtn.addEventListener("click", () => {
                window.open(
                    `${window.location.origin}/?mode=obs`,
                    "MyAgent_OBS_Overlay",
                    "width=900,height=920,menubar=no,toolbar=no,location=no,status=no"
                );
            });
        }

        // ユーザーチャット送信フォーム
        const chatForm = document.getElementById("chat-form");
        const chatInput = document.getElementById("chat-input");
        if (chatForm && chatInput) {
            chatForm.addEventListener("submit", (e) => {
                e.preventDefault();
                const text = chatInput.value.trim();
                if (!text) return;
                this.sendChatMessage(text, "user_text", "Master");
                chatInput.value = "";
            });
        }

        // クイック自律コマンドボタン
        document.querySelectorAll(".quick-cmd-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                if (btn.dataset.action === "vision") {
                    this.triggerVisionAnalyze();
                    return;
                }
                const prompt = btn.dataset.prompt;
                if (prompt) {
                    this.sendChatMessage(prompt, "user_text", "Master");
                }
            });
        });

        // ファイル選択ダイアログからの解析
        const fileInput = document.getElementById("file-input-hidden");
        if (fileInput) {
            fileInput.addEventListener("change", (e) => {
                const file = e.target.files && e.target.files[0];
                if (file) {
                    this._handleDroppedFile(file);
                    fileInput.value = "";
                }
            });
        }

        // マイク音声入力ボタン
        const micBtn = document.getElementById("btn-mic");
        if (micBtn) {
            micBtn.addEventListener("click", () => {
                this.toggleMicListening();
            });
        }

        // VTuberディレクター：話題デッキ発動＆BGMトグル＆模擬ストリーム＆スパチャ
        const deckBtn = document.getElementById("btn-next-talk-deck");
        if (deckBtn) {
            deckBtn.addEventListener("click", () => {
                fetch("/api/talk_deck/next", { method: "POST" });
            });
        }

        const bgmBtn = document.getElementById("btn-toggle-bgm");
        if (bgmBtn) {
            bgmBtn.addEventListener("click", () => {
                this.toggleBgm();
            });
        }

        const demoStreamBtn = document.getElementById("btn-demo-stream");
        if (demoStreamBtn) {
            demoStreamBtn.addEventListener("click", async () => {
                await fetch("/api/demo_stream", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
            });
        }

        const sendCommentBtn = document.getElementById("btn-send-viewer-comment");
        if (sendCommentBtn) {
            sendCommentBtn.addEventListener("click", () => {
                const viewerName = document.getElementById("viewer-name-input").value.trim() || "名無しのリスナー";
                const commentInput = document.getElementById("viewer-comment-input");
                const text = commentInput.value.trim() || "ニアちゃん初見です！配信頑張って！";
                this.sendChatMessage(text, "live_comment", viewerName);
                commentInput.value = "";
            });
        }

        const sendSuperchatBtn = document.getElementById("btn-send-superchat");
        if (sendSuperchatBtn) {
            sendSuperchatBtn.addEventListener("click", () => {
                const viewerName = document.getElementById("viewer-name-input").value.trim() || "石油王リスナー";
                const commentInput = document.getElementById("viewer-comment-input");
                const text = commentInput.value.trim() || "いつも神対応ありがとう！拡張GPUサーバー代です！！";
                this.sendChatMessage(text, "superchat", viewerName, { amount: "¥10,000" });
                commentInput.value = "";
            });
        }

        // クイック設定変更（Ollamaモデル・TTS・Proactive切替）
        const modelSelect = document.getElementById("select-ollama-model");
        if (modelSelect) {
            modelSelect.addEventListener("change", () => {
                const isVtuber = (this.currentConfig && this.currentConfig.mode) === "vtuber";
                this.updateConfig(
                    isVtuber
                        ? { ollama_model_vtuber: modelSelect.value }
                        : { ollama_model: modelSelect.value }
                );
            });
        }
        const ttsSelect = document.getElementById("select-tts-provider");
        if (ttsSelect) {
            ttsSelect.addEventListener("change", () => {
                this.updateConfig({ tts_provider: ttsSelect.value });
            });
        }
        const proactiveCheck = document.getElementById("check-proactive");
        if (proactiveCheck) {
            proactiveCheck.addEventListener("change", () => {
                this.updateConfig({ proactive_enabled: proactiveCheck.checked });
            });
        }
        const discordAutonomousCheck = document.getElementById("check-discord-autonomous");
        if (discordAutonomousCheck) {
            discordAutonomousCheck.addEventListener("change", () => {
                this.updateConfig({ discord_notify_autonomous: discordAutonomousCheck.checked });
            });
        }

        // === Agent 自律運用（ワークフロー・RAG・Deep Research・MCPテスト） ===
        const toggleAddWfBtn = document.getElementById("btn-toggle-add-wf");
        const addWfBox = document.getElementById("form-add-workflow");
        if (toggleAddWfBtn && addWfBox) {
            toggleAddWfBtn.addEventListener("click", () => addWfBox.classList.toggle("hidden"));
        }
        const submitWfBtn = document.getElementById("btn-submit-wf");
        if (submitWfBtn) {
            submitWfBtn.addEventListener("click", async () => {
                const name = document.getElementById("wf-name").value.trim() || "定期タスク";
                const workflow_type = document.getElementById("wf-type").value;
                const schedule_kind = document.getElementById("wf-sched-kind").value;
                const schedule_value = document.getElementById("wf-sched-val").value.trim() || "08:00";
                const target_url = document.getElementById("wf-url").value.trim();
                const prompt = document.getElementById("wf-prompt").value.trim();
                await fetch("/api/agent/workflows", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name, workflow_type, schedule_kind, schedule_value, target_url, prompt }),
                });
                document.getElementById("wf-name").value = "";
                document.getElementById("wf-url").value = "";
                document.getElementById("wf-prompt").value = "";
                if (addWfBox) addWfBox.classList.add("hidden");
            });
        }
        const reindexRagBtn = document.getElementById("btn-reindex-rag");
        if (reindexRagBtn) {
            reindexRagBtn.addEventListener("click", async () => {
                reindexRagBtn.disabled = true;
                try {
                    const res = await fetch("/api/agent/rag/reindex", { method: "POST" }).then((r) => r.json());
                    this._appendThoughtItem(
                        `[ローカルRAG索引完了] 新規/更新:${res.indexed_files}件 / 合計ドキュメント:${res.total_docs}件 (${res.total_chunks}チャンク)`,
                        "tool"
                    );
                } finally {
                    reindexRagBtn.disabled = false;
                }
            });
        }
        const startResearchBtn = document.getElementById("btn-start-research");
        if (startResearchBtn) {
            startResearchBtn.addEventListener("click", async () => {
                const inp = document.getElementById("input-research-topic");
                const topic = inp ? inp.value.trim() : "";
                if (!topic) return;
                inp.value = "";
                await fetch("/api/agent/research", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ topic }),
                });
                this._appendThoughtItem(`[Deep Research] 「${topic}」のバックグラウンド調査を開始しました。`, "tool");
            });
        }
        const testMcpBtn = document.getElementById("btn-test-mcp");
        if (testMcpBtn) {
            testMcpBtn.addEventListener("click", async () => {
                await this.updateConfig({ mcp_servers_json: document.getElementById("cfg-mcp-json").value });
                const res = await fetch("/api/agent/mcp/list", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                }).then((r) => r.json());
                alert(res.result || "応答なし");
            });
        }

        // === VTuber 配信スタジオ拡張 (BGMシーン / セッション記録 / OBS / VTS / 投票 / お便り / 歌枠) ===
        const bgmSceneSelect = document.getElementById("select-bgm-scene");
        if (bgmSceneSelect) {
            bgmSceneSelect.addEventListener("change", async () => {
                await this.updateConfig({ obs_bgm_scene: bgmSceneSelect.value });
                if (this.isBgmPlaying) {
                    await this.toggleBgm(false);
                    await this.toggleBgm(true);
                }
            });
        }
        const streamSessBtn = document.getElementById("btn-stream-session-toggle");
        if (streamSessBtn) {
            streamSessBtn.addEventListener("click", async () => {
                const isLive = !!(this.currentConfig && this.currentConfig.active_stream_session);
                if (isLive) {
                    const res = await fetch("/api/vtuber/session/end", { method: "POST" }).then((r) => r.json());
                    if (res.ok) {
                        this._appendThoughtItem(`[配信アーカイブ保存] ${res.archive_path}`, "tool");
                        this._appendFeedItem({
                            role: "agent",
                            user: "配信アーカイブ生成",
                            emotion: "smile",
                            content: `📺 配信お疲れ様！チャプターとX告知案を作成したわ。\n\n⏱️ チャプター:\n${res.chapters_text}\n\n🐦 X投稿案:\n${res.x_post}`,
                        });
                    }
                } else {
                    const topic = (document.getElementById("input-stream-topic")?.value || "").trim() || "ニアのライブ配信";
                    await fetch("/api/vtuber/session/start", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ title: topic }),
                    });
                }
            });
        }
        document.querySelectorAll(".obs-scene-btn").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const scene = btn.dataset.scene;
                const res = await fetch("/api/vtuber/obs/scene", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ scene }),
                }).then((r) => r.json());
                this._appendThoughtItem(
                    res.ok ? `[OBSシーン切替] ${scene} へ切り替えました。` : `[OBS連携] ${res.error}`,
                    "tool"
                );
            });
        });
        const pollStartBtn = document.getElementById("btn-poll-start");
        if (pollStartBtn) {
            pollStartBtn.addEventListener("click", async () => {
                const game_type = document.getElementById("poll-game-type").value;
                const title = document.getElementById("poll-title-input").value.trim() || "今日の二択アンケート";
                const optsRaw = document.getElementById("poll-options-input").value.trim();
                const options = optsRaw ? optsRaw.split(/[,、]/).map((s) => s.trim()).filter(Boolean) : [];
                await fetch("/api/vtuber/poll/start", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ game_type, title, options, duration_sec: 45 }),
                });
            });
        }
        const pollStopBtn = document.getElementById("btn-poll-stop");
        if (pollStopBtn) {
            pollStopBtn.addEventListener("click", () => fetch("/api/vtuber/poll/stop", { method: "POST" }));
        }
        const addMailbagBtn = document.getElementById("btn-add-mailbag");
        if (addMailbagBtn) {
            addMailbagBtn.addEventListener("click", async () => {
                const inp = document.getElementById("mailbag-input");
                const question = inp ? inp.value.trim() : "";
                if (!question) return;
                inp.value = "";
                const sender = document.getElementById("viewer-name-input")?.value.trim() || "匿名リスナー";
                await fetch("/api/vtuber/mailbag", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ sender, question }),
                });
                this._appendThoughtItem(`[お便り追加] 「${question.slice(0, 40)}」`, "tool");
            });
        }
        const readMailbagBtn = document.getElementById("btn-read-mailbag");
        if (readMailbagBtn) {
            readMailbagBtn.addEventListener("click", async () => {
                const res = await fetch("/api/vtuber/mailbag/read_next", { method: "POST" }).then((r) => r.json());
                if (!res.ok) alert(res.error || "未読のお便りがありません");
            });
        }
        const playKaraokeBtn = document.getElementById("btn-play-karaoke");
        if (playKaraokeBtn) {
            playKaraokeBtn.addEventListener("click", async () => {
                const sel = document.getElementById("select-karaoke-track");
                const track_id = sel ? sel.value : "nia_stage_demo";
                await fetch("/api/vtuber/karaoke/play", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ track_id }),
                });
            });
        }
        const testObsBtn = document.getElementById("btn-test-obs-ws");
        if (testObsBtn) {
            testObsBtn.addEventListener("click", async () => {
                await this.updateConfig(this._collectAllSettingsFromModal());
                const res = await fetch("/api/vtuber/obs/test", { method: "POST" }).then((r) => r.json());
                alert(res.ok ? `OBS接続成功！シーン一覧: ${(res.scenes || []).join(", ")}` : `OBS接続エラー: ${res.error}`);
            });
        }
        const testVtsBtn = document.getElementById("btn-test-vts");
        if (testVtsBtn) {
            testVtsBtn.addEventListener("click", async () => {
                await this.updateConfig(this._collectAllSettingsFromModal());
                const res = await fetch("/api/vtuber/vts/test", { method: "POST" }).then((r) => r.json());
                alert(res.ok ? `VTube Studio 接続成功！ホットキー数: ${(res.hotkeys || []).length}` : `VTSエラー: ${res.error}`);
            });
        }
    }

    /**
     * ファイルドラッグ＆ドロップによる即時要約・コード解析・画像Vision解析をバインドする。
     */
    _bindDragAndDrop() {
        const stage = document.getElementById("center-stage-dropzone");
        const overlay = document.getElementById("drop-overlay");
        if (!stage || !overlay) return;

        let hideTimer = null;
        const hideOverlay = () => {
            if (hideTimer) {
                clearTimeout(hideTimer);
                hideTimer = null;
            }
            overlay.classList.add("hidden");
        };
        const scheduleHide = () => {
            if (hideTimer) clearTimeout(hideTimer);
            hideTimer = setTimeout(hideOverlay, 450);
        };
        const hasDraggedFiles = (e) => {
            const types = e.dataTransfer && e.dataTransfer.types;
            return types ? Array.from(types).includes("Files") : false;
        };

        const isAnyModalOpen = () => {
            const m1 = document.getElementById("outfit-upload-modal");
            const m2 = document.getElementById("persona-modal");
            return (m1 && !m1.classList.contains("hidden")) || (m2 && !m2.classList.contains("hidden"));
        };

        window.addEventListener("dragover", (e) => {
            if (!hasDraggedFiles(e)) return;
            e.preventDefault();
            if (isAnyModalOpen()) {
                hideOverlay();
                return;
            }
            overlay.classList.remove("hidden");
            scheduleHide();
        });
        window.addEventListener("dragleave", (e) => {
            if (
                !e.relatedTarget ||
                e.clientX <= 0 ||
                e.clientY <= 0 ||
                e.clientX >= window.innerWidth ||
                e.clientY >= window.innerHeight
            ) {
                hideOverlay();
            }
        });
        window.addEventListener("dragend", hideOverlay);
        window.addEventListener("blur", hideOverlay);
        overlay.addEventListener("click", hideOverlay);
        window.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && !overlay.classList.contains("hidden")) {
                hideOverlay();
            }
        });
        window.addEventListener("drop", (e) => {
            e.preventDefault();
            hideOverlay();
            if (isAnyModalOpen()) return;
            const files = e.dataTransfer && e.dataTransfer.files;
            if (files && files.length > 0) {
                this._handleDroppedFile(files[0]);
            }
        });
    }

    _handleDroppedFile(file) {
        const lowerName = (file.name || "").toLowerCase();
        const isImage = file.type.startsWith("image/") || /\.(png|jpg|jpeg|webp|gif|bmp)$/.test(lowerName);
        const isBinaryDocOrAudio =
            file.type.startsWith("audio/") ||
            /\.(pdf|docx|xlsx|pptx|mp3|wav|ogg|oga|m4a|flac|webm)$/.test(lowerName);
        const reader = new FileReader();
        if (isImage) {
            reader.onload = async () => {
                await fetch("/api/upload_analyze", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        filename: file.name,
                        image_base64: reader.result,
                        prompt: "ドロップされた画像ファイルの内容を詳しく確認して解説して",
                    }),
                });
            };
            reader.readAsDataURL(file);
        } else if (isBinaryDocOrAudio) {
            reader.onload = async () => {
                const res = await fetch("/api/upload_analyze", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        filename: file.name,
                        file_base64: reader.result,
                        prompt: "ドロップされた資料/音声ファイルの内容を読み取って要約・報告して",
                    }),
                }).then((r) => r.json());
                if (res && !res.ok && res.error) {
                    this._appendThoughtItem(`[ファイル解析エラー] ${res.error}`, "tool");
                }
            };
            reader.readAsDataURL(file);
        } else {
            reader.onload = async () => {
                await fetch("/api/upload_analyze", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        filename: file.name,
                        text_content: String(reader.result || ""),
                        prompt: "ドロップされたファイルの内容を要約・分析して報告して",
                    }),
                });
            };
            reader.readAsText(file);
        }
    }

    /**
     * ショートカットキー（デフォルト Alt+Space）でマイク入力またはチャット入力欄を呼び出す。
     */
    _bindGlobalHotkeys() {
        window.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                if (this.avatar.isSpeaking || this.currentAudio) {
                    this.stopCurrentSpeech();
                    fetch("/api/speech/stop", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ clear_queue: false }),
                    });
                }
                return;
            }
            if (e.altKey && e.code === "Space") {
                e.preventDefault();
                const chatInput = document.getElementById("chat-input");
                if (document.activeElement === chatInput) {
                    this.toggleMicListening();
                } else if (chatInput) {
                    chatInput.focus();
                }
            }
        });
    }

    /**
     * 画面キャプチャ＆視覚解析を実行する（OSネイティブキャプチャ＋ブラウザScreen Capture APIハイブリッド）。
     */
    async triggerVisionAnalyze() {
        this._appendThoughtItem("[Vision] 現在の画面をキャプチャして解析中...", "tool");
        const res = await fetch("/api/vision_analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        }).then((r) => r.json());

        if (res.ok && res.capture) {
            this._appendThoughtItem(
                `[Visionキャプチャ完了] 対象: ${res.capture.window_title} (${res.capture.resolution})`,
                "observation"
            );
            return;
        }

        // バックエンドプロセスが非対話セッション等でOS直接キャプチャ不可の場合、ブラウザScreen Capture APIで取得
        if (navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) {
            try {
                const stream = await navigator.mediaDevices.getDisplayMedia({
                    video: { cursor: "always" },
                    audio: false,
                });
                const track = stream.getVideoTracks()[0];
                const video = document.createElement("video");
                video.srcObject = stream;
                await video.play();

                const capCanvas = document.createElement("canvas");
                const maxDim = 1280;
                const scale = Math.min(1.0, maxDim / Math.max(video.videoWidth || 1280, video.videoHeight || 720));
                capCanvas.width = Math.round((video.videoWidth || 1280) * scale);
                capCanvas.height = Math.round((video.videoHeight || 720) * scale);
                const cctx = capCanvas.getContext("2d");
                cctx.drawImage(video, 0, 0, capCanvas.width, capCanvas.height);
                track.stop();

                const dataUrl = capCanvas.toDataURL("image/jpeg", 0.84);
                await fetch("/api/upload_analyze", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        filename: "screen_capture.jpg",
                        image_base64: dataUrl,
                        prompt: "現在キャプチャしたPC画面の内容を確認して、状況の要約やアドバイスを教えて",
                    }),
                });
                this._appendThoughtItem(
                    `[Visionキャプチャ完了] ブラウザ画面共有経由 (${capCanvas.width}x${capCanvas.height})`,
                    "observation"
                );
                return;
            } catch (err) {
                this._appendThoughtItem(`[Visionキャンセル/エラー] ${err.message || err}`, "observation");
                return;
            }
        }

        this._appendThoughtItem(`[Visionエラー] ${res.error}`, "observation");
    }

    /**
     * 全5タブの設定モーダルに現在の設定値を流し込む。
     */
    _populatePersonaModal() {
        const c = this.currentConfig;
        const setVal = (id, v) => {
            const el = document.getElementById(id);
            if (el && v !== undefined && v !== null) el.value = v;
        };
        const setCheck = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.checked = !!v;
        };
        const setText = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.textContent = v;
        };

        // タブ1: ペルソナ
        setVal("input-agent-name", c.agent_name || "ニア (NIA)");
        setVal("textarea-persona-agent", c.persona_agent || "");
        setVal("textarea-persona-vtuber", c.persona_vtuber || "");

        // タブ2: マスコット・視覚・監視
        setVal("cfg-mascot-scale", c.mascot_scale ?? 1.0);
        setVal("cfg-mascot-position", c.mascot_position || "bottom-right");
        setVal("cfg-vision-target", c.vision_target || "active_window");
        setVal("cfg-window-watch-sec", c.window_watch_interval_sec ?? 180);
        setVal("cfg-pomo-work", c.pomodoro_work_min ?? 25);
        setVal("cfg-pomo-break", c.pomodoro_break_min ?? 5);
        setVal("cfg-hotkey-ptt", c.hotkey_ptt || "Alt+Space");
        setVal("cfg-wake-word", c.wake_word || "ニア");
        setVal("cfg-vision-exclude", (c.vision_exclude_apps || []).join(", "));
        setCheck("cfg-mascot-topmost", c.mascot_always_on_top ?? true);
        setCheck("cfg-mascot-clickthrough", c.mascot_click_through ?? false);
        setCheck("cfg-window-watch", c.window_watch_enabled ?? true);
        setCheck("cfg-dnd-fullscreen", c.dnd_fullscreen_mute ?? true);
        setCheck("cfg-wake-required", c.wake_word_required ?? false);
        setCheck("cfg-clipboard-watch", c.clipboard_watch_enabled ?? false);

        // タブ3: 配信・企画・NG検閲
        setVal("cfg-youtube-id", c.youtube_live_id || "");
        setVal("cfg-twitch-ch", c.twitch_channel || "");
        setVal("cfg-live-poll", c.live_poll_interval_sec ?? 6);
        setVal("cfg-live-queue", c.live_queue_limit ?? 8);
        setVal("cfg-silence-sec", c.silence_timeout_sec ?? 90);
        setCheck("cfg-talk-auto-news", c.talk_deck_auto_news ?? true);
        setCheck("cfg-talk-auto-advance", !!c.talk_deck_auto_advance);
        setVal("cfg-talk-deck", (c.talk_deck_themes || []).join("\n"));
        setVal("cfg-ng-words", (c.ng_words || []).join(", "));
        setVal("cfg-ng-action", c.ng_action || "deflect");

        // タブ4: ボイス・辞書・OBS演出
        setVal("cfg-edge-voice", c.edge_tts_voice || "ja-JP-NanamiNeural");
        setVal("cfg-vv-speaker", c.voicevox_speaker_id ?? 3);
        setVal("cfg-aivis-speaker", c.aivisspeech_speaker_id ?? 888753760);
        setVal("cfg-tts-vol", c.tts_volume ?? 1.0);
        setText("val-tts-vol", c.tts_volume ?? 1.0);
        setVal("cfg-tts-speed", c.tts_speed ?? 1.08);
        setText("val-tts-speed", c.tts_speed ?? 1.08);
        setVal("cfg-tts-pitch", c.tts_pitch ?? 1.0);
        setText("val-tts-pitch", c.tts_pitch ?? 1.0);
        setVal("cfg-tts-into", c.tts_intonation ?? 1.1);
        setText("val-tts-into", c.tts_intonation ?? 1.1);

        const dictLines = (c.pronunciation_dict || []).map((d) => `${d.word}=${d.reading}`);
        setVal("cfg-pronunciation-dict", dictLines.join("\n"));

        setVal("cfg-obs-motion", c.obs_motion_intensity ?? 1.0);
        setText("val-obs-motion", c.obs_motion_intensity ?? 1.0);
        setVal("cfg-obs-sub", c.obs_subtitle_scale ?? 1.0);
        setText("val-obs-sub", c.obs_subtitle_scale ?? 1.0);
        setVal("cfg-bgm-vol", c.obs_bgm_volume ?? 0.18);
        setText("val-bgm-vol", c.obs_bgm_volume ?? 0.18);
        setVal("cfg-bgm-duck", c.obs_bgm_ducking_ratio ?? 0.25);
        setText("val-bgm-duck", c.obs_bgm_ducking_ratio ?? 0.25);
        setCheck("cfg-obs-se", c.obs_se_enabled ?? true);
        setCheck("cfg-obs-bgm", c.obs_bgm_enabled ?? false);

        // タブ5: 知能・記憶・予定表
        setVal("cfg-fetch-chars", c.fetch_max_chars ?? 4000);
        setVal("cfg-fetch-timeout", c.fetch_timeout_sec ?? 10);
        setVal("cfg-mem-limit", c.memory_prompt_limit ?? 8);
        setVal("cfg-history-limit", c.conversation_history_limit ?? 12);
        setVal("cfg-snooze-min", c.schedule_snooze_min ?? 5);
        setVal("cfg-llm-routing", c.llm_routing_mode || "auto");
        setVal("cfg-model-fast", c.ollama_model_fast || "gemma4:e4b");
        setVal("cfg-model-smart", c.ollama_model_smart || "gemma4:26b");
        setVal("cfg-bilingual-lang", c.bilingual_target_lang || "en");
        setCheck("cfg-auto-mem", c.auto_memory_extract ?? true);
        setCheck("cfg-sched-voice", c.schedule_voice_notify ?? true);
        setCheck("cfg-local-launch", c.local_launch_enabled ?? true);
        setCheck("cfg-self-review", c.llm_self_review_enabled ?? true);
        setCheck("cfg-stream-tts", c.streaming_tts_enabled ?? true);
        setCheck("cfg-semantic-mem", c.semantic_memory_enabled ?? true);
        setCheck("cfg-auto-consolidate", c.memory_auto_consolidate ?? true);
        setCheck("cfg-bilingual-enabled", !!c.bilingual_enabled);
        const launchLines = (c.local_launch_allowlist || []).map(
            (e) => `${e.name}=${e.type}=${e.target}`
        );
        setVal("cfg-local-launch-list", launchLines.join("\n"));

        // タブ3/4 拡張項目
        setCheck("cfg-viewer-profile", c.viewer_profile_enabled ?? true);
        setCheck("cfg-obs-topic-telop", c.obs_topic_telop_enabled ?? true);
        setCheck("cfg-obs-telop-pomo", c.obs_telop_show_pomodoro ?? true);
        setVal("cfg-cache-files", c.audio_cache_max_files ?? 30);
        setVal("cfg-cache-age", c.audio_cache_max_age_min ?? 30);

        // タブ6: Discord Bot 双方向連携
        setCheck("cfg-discord-enabled", !!c.discord_enabled);
        setCheck("cfg-discord-proactive", c.discord_notify_proactive ?? true);
        setCheck("cfg-discord-autonomous", !!c.discord_notify_autonomous);
        setCheck("cfg-discord-image", c.discord_send_vision_image ?? true);
        setCheck("cfg-discord-voice", !!c.discord_send_voice);
        const tokInput = document.getElementById("cfg-discord-token");
        if (tokInput) {
            tokInput.value = "";
            tokInput.placeholder = c.has_discord_token
                ? "設定済み（変更する場合のみ新しいトークンを入力）"
                : "Discord Botトークンを入力...";
        }
        setVal("cfg-discord-users", (c.discord_allowed_user_ids || []).join(", "));
        setVal("cfg-discord-channels", (c.discord_allowed_channel_ids || []).join(", "));
        setVal("cfg-discord-notify-user", c.discord_notify_user_id || "");
        setCheck("cfg-discord-thread-iso", c.discord_thread_isolation ?? true);
        this._renderDiscordStatus(c.discord_status);

        // タブ7: AIエージェント高度拡張
        setCheck("cfg-file-tools", c.file_tools_enabled ?? true);
        setCheck("cfg-cmd-exec", c.command_execution_enabled ?? true);
        setCheck("cfg-rag-enabled", c.rag_enabled ?? true);
        setCheck("cfg-workflow-enabled", c.workflow_enabled ?? true);
        setCheck("cfg-playwright-enabled", c.playwright_enabled ?? true);
        setCheck("cfg-smart-hooks", c.smart_hooks_enabled ?? true);
        setCheck("cfg-toast-watch", !!c.toast_watch_enabled);
        setCheck("cfg-win-power-ctrl", c.windows_power_control_enabled ?? true);
        setCheck("cfg-local-stt", !!c.local_stt_enabled);
        setVal("cfg-cmd-policy", c.command_approval_mode || "always");
        setVal("cfg-whisper-model", c.whisper_model_size || "tiny");
        setVal("cfg-github-repo", c.github_default_repo || "");
        const ghTok = document.getElementById("cfg-github-token");
        if (ghTok) {
            ghTok.value = "";
            ghTok.placeholder = c.has_github_token ? "設定済み（変更時のみ入力）" : "GitHub Token (任意)";
        }
        setVal("cfg-google-ics", c.google_ics_url || "");
        setVal("cfg-file-dirs", (c.file_allowed_dirs || []).join("\n"));
        setVal("cfg-rag-dirs", (c.rag_dirs || []).join("\n"));
        setVal("cfg-mcp-json", c.mcp_servers_json || "[]");

        // タブ8: VTuber配信拡張
        setCheck("cfg-live-batch", c.live_batch_enabled ?? true);
        setCheck("cfg-vtuber-news-card", c.vtuber_news_card_enabled ?? true);
        setCheck("cfg-vtuber-screen-watch", !!c.vtuber_screen_commentary_enabled);
        setCheck("cfg-vtuber-collab", !!c.vtuber_collab_mode);
        setCheck("cfg-obs-ws-enabled", !!c.obs_ws_enabled);
        setCheck("cfg-vts-enabled", !!c.vts_enabled);
        setVal("cfg-obs-host", c.obs_ws_host || "127.0.0.1");
        setVal("cfg-obs-port", c.obs_ws_port ?? 4455);
        const obsPass = document.getElementById("cfg-obs-pass");
        if (obsPass) {
            obsPass.value = "";
            obsPass.placeholder = c.has_obs_password ? "設定済み（変更時のみ入力）" : "未設定時は空欄";
        }
        setVal("cfg-vts-url", c.vts_ws_url || "ws://127.0.0.1:8001");
        setVal("cfg-vtuber-screen-sec", c.vtuber_screen_watch_interval_sec ?? 25);
        setVal("cfg-vtuber-window-filter", c.vtuber_watch_window_filter || "");
        setVal("cfg-vtuber-blocklist", (c.vtuber_private_blocklist || []).join(", "));
        const sm = c.obs_scene_map || {};
        setVal("cfg-obs-scene-talk", sm.talk || "雑談");
        setVal("cfg-obs-scene-news", sm.news || "ニュース");
        setVal("cfg-obs-scene-game", sm.game || "ゲーム");
        setVal("cfg-obs-scene-karaoke", sm.karaoke || "歌枠");
        setVal("cfg-obs-scene-brb", sm.brb || "離席中");
    }

    /**
     * モーダル内の全5タブの入力値を収集して設定更新オブジェクトを生成する。
     */
    _collectAllSettingsFromModal() {
        const val = (id) => {
            const el = document.getElementById(id);
            return el ? el.value : "";
        };
        const chk = (id) => {
            const el = document.getElementById(id);
            return el ? el.checked : false;
        };

        const dictRaw = val("cfg-pronunciation-dict")
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean);
        const pronunciation_dict = [];
        for (const line of dictRaw) {
            const idx = line.indexOf("=");
            if (idx > 0) {
                pronunciation_dict.push({
                    word: line.slice(0, idx).trim(),
                    reading: line.slice(idx + 1).trim(),
                });
            }
        }

        const launchLines = val("cfg-local-launch-list")
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean);
        const local_launch_allowlist = [];
        for (const line of launchLines) {
            const parts = line.split("=");
            if (parts.length >= 3) {
                const name = parts[0].trim();
                const ltype = parts[1].trim().toLowerCase();
                const target = parts.slice(2).join("=").trim();
                if (name && target && ["app", "url", "folder"].includes(ltype)) {
                    local_launch_allowlist.push({ name, type: ltype, target });
                }
            }
        }

        const parsedHistoryLimit = parseInt(val("cfg-history-limit"), 10);
        const discordToken = val("cfg-discord-token").trim();

        const collected = {
            agent_name: val("input-agent-name"),
            persona_agent: val("textarea-persona-agent"),
            persona_vtuber: val("textarea-persona-vtuber"),
            mascot_scale: parseFloat(val("cfg-mascot-scale")) || 1.0,
            mascot_position: val("cfg-mascot-position"),
            vision_target: val("cfg-vision-target"),
            window_watch_interval_sec: parseInt(val("cfg-window-watch-sec"), 10) || 180,
            pomodoro_work_min: parseInt(val("cfg-pomo-work"), 10) || 25,
            pomodoro_break_min: parseInt(val("cfg-pomo-break"), 10) || 5,
            hotkey_ptt: val("cfg-hotkey-ptt"),
            wake_word: val("cfg-wake-word"),
            vision_exclude_apps: val("cfg-vision-exclude")
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            mascot_always_on_top: chk("cfg-mascot-topmost"),
            mascot_click_through: chk("cfg-mascot-clickthrough"),
            window_watch_enabled: chk("cfg-window-watch"),
            dnd_fullscreen_mute: chk("cfg-dnd-fullscreen"),
            wake_word_required: chk("cfg-wake-required"),
            clipboard_watch_enabled: chk("cfg-clipboard-watch"),
            youtube_live_id: val("cfg-youtube-id"),
            twitch_channel: val("cfg-twitch-ch"),
            live_poll_interval_sec: parseInt(val("cfg-live-poll"), 10) || 6,
            live_queue_limit: parseInt(val("cfg-live-queue"), 10) || 8,
            silence_timeout_sec: parseInt(val("cfg-silence-sec"), 10) || 90,
            talk_deck_auto_news: chk("cfg-talk-auto-news"),
            talk_deck_auto_advance: chk("cfg-talk-auto-advance"),
            talk_deck_themes: val("cfg-talk-deck")
                .split(/\r?\n/)
                .map((s) => s.trim())
                .filter(Boolean),
            ng_words: val("cfg-ng-words")
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            ng_action: val("cfg-ng-action"),
            edge_tts_voice: val("cfg-edge-voice"),
            voicevox_speaker_id: parseInt(val("cfg-vv-speaker"), 10) || 3,
            aivisspeech_speaker_id: parseInt(val("cfg-aivis-speaker"), 10) || 888753760,
            tts_volume: parseFloat(val("cfg-tts-vol")) || 1.0,
            tts_speed: parseFloat(val("cfg-tts-speed")) || 1.08,
            tts_pitch: parseFloat(val("cfg-tts-pitch")) || 1.0,
            tts_intonation: parseFloat(val("cfg-tts-into")) || 1.1,
            pronunciation_dict,
            obs_motion_intensity: parseFloat(val("cfg-obs-motion")) ?? 1.0,
            obs_subtitle_scale: parseFloat(val("cfg-obs-sub")) || 1.0,
            obs_bgm_volume: parseFloat(val("cfg-bgm-vol")) || 0.18,
            obs_bgm_ducking_ratio: parseFloat(val("cfg-bgm-duck")) || 0.25,
            obs_se_enabled: chk("cfg-obs-se"),
            obs_bgm_enabled: chk("cfg-obs-bgm"),
            fetch_max_chars: parseInt(val("cfg-fetch-chars"), 10) || 4000,
            fetch_timeout_sec: parseInt(val("cfg-fetch-timeout"), 10) || 10,
            memory_prompt_limit: parseInt(val("cfg-mem-limit"), 10) || 8,
            conversation_history_limit: Number.isFinite(parsedHistoryLimit) ? parsedHistoryLimit : 12,
            schedule_snooze_min: parseInt(val("cfg-snooze-min"), 10) || 5,
            llm_routing_mode: val("cfg-llm-routing") || "auto",
            ollama_model_fast: val("cfg-model-fast").trim() || "gemma4:e4b",
            ollama_model_smart: val("cfg-model-smart").trim() || "gemma4:26b",
            bilingual_target_lang: val("cfg-bilingual-lang") || "en",
            auto_memory_extract: chk("cfg-auto-mem"),
            schedule_voice_notify: chk("cfg-sched-voice"),
            llm_self_review_enabled: chk("cfg-self-review"),
            streaming_tts_enabled: chk("cfg-stream-tts"),
            semantic_memory_enabled: chk("cfg-semantic-mem"),
            memory_auto_consolidate: chk("cfg-auto-consolidate"),
            bilingual_enabled: chk("cfg-bilingual-enabled"),
            viewer_profile_enabled: chk("cfg-viewer-profile"),
            obs_topic_telop_enabled: chk("cfg-obs-topic-telop"),
            obs_telop_show_pomodoro: chk("cfg-obs-telop-pomo"),
            audio_cache_max_files: parseInt(val("cfg-cache-files"), 10) || 30,
            audio_cache_max_age_min: parseInt(val("cfg-cache-age"), 10) || 30,
            local_launch_enabled: chk("cfg-local-launch"),
            local_launch_allowlist,
            discord_enabled: chk("cfg-discord-enabled"),
            discord_notify_proactive: chk("cfg-discord-proactive"),
            discord_notify_autonomous: chk("cfg-discord-autonomous"),
            discord_send_vision_image: chk("cfg-discord-image"),
            discord_send_voice: chk("cfg-discord-voice"),
            discord_allowed_user_ids: val("cfg-discord-users")
                .split(/[\s,]+/)
                .map((s) => s.trim())
                .filter(Boolean),
            discord_allowed_channel_ids: val("cfg-discord-channels")
                .split(/[\s,]+/)
                .map((s) => s.trim())
                .filter(Boolean),
            discord_notify_user_id: val("cfg-discord-notify-user").trim(),
            discord_thread_isolation: chk("cfg-discord-thread-iso"),
            file_tools_enabled: chk("cfg-file-tools"),
            command_execution_enabled: chk("cfg-cmd-exec"),
            rag_enabled: chk("cfg-rag-enabled"),
            workflow_enabled: chk("cfg-workflow-enabled"),
            playwright_enabled: chk("cfg-playwright-enabled"),
            smart_hooks_enabled: chk("cfg-smart-hooks"),
            toast_watch_enabled: chk("cfg-toast-watch"),
            windows_power_control_enabled: chk("cfg-win-power-ctrl"),
            local_stt_enabled: chk("cfg-local-stt"),
            command_approval_mode: val("cfg-cmd-policy") || "always",
            whisper_model_size: val("cfg-whisper-model") || "tiny",
            github_default_repo: val("cfg-github-repo").trim(),
            google_ics_url: val("cfg-google-ics").trim(),
            file_allowed_dirs: val("cfg-file-dirs"),
            rag_dirs: val("cfg-rag-dirs"),
            mcp_servers_json: val("cfg-mcp-json").trim() || "[]",
            live_batch_enabled: chk("cfg-live-batch"),
            vtuber_news_card_enabled: chk("cfg-vtuber-news-card"),
            vtuber_screen_commentary_enabled: chk("cfg-vtuber-screen-watch"),
            vtuber_collab_mode: chk("cfg-vtuber-collab"),
            obs_ws_enabled: chk("cfg-obs-ws-enabled"),
            vts_enabled: chk("cfg-vts-enabled"),
            obs_ws_host: val("cfg-obs-host").trim() || "127.0.0.1",
            obs_ws_port: parseInt(val("cfg-obs-port"), 10) || 4455,
            vts_ws_url: val("cfg-vts-url").trim() || "ws://127.0.0.1:8001",
            vtuber_screen_watch_interval_sec: parseInt(val("cfg-vtuber-screen-sec"), 10) || 25,
            vtuber_watch_window_filter: val("cfg-vtuber-window-filter").trim(),
            vtuber_private_blocklist: val("cfg-vtuber-blocklist"),
            obs_scene_map: {
                talk: val("cfg-obs-scene-talk").trim() || "雑談",
                news: val("cfg-obs-scene-news").trim() || "ニュース",
                game: val("cfg-obs-scene-game").trim() || "ゲーム",
                karaoke: val("cfg-obs-scene-karaoke").trim() || "歌枠",
                brb: val("cfg-obs-scene-brb").trim() || "離席中",
            },
        };
        if (discordToken) {
            collected.discord_bot_token = discordToken;
        }
        const ghToken = val("cfg-github-token").trim();
        if (ghToken) {
            collected.github_token = ghToken;
        }
        const obsPass = val("cfg-obs-pass").trim();
        if (obsPass) {
            collected.obs_ws_password = obsPass;
        }
        return collected;
    }

    /**
     * WebSocketサーバーへ接続し、メッセージハンドラを設定する。
     */
    _connectWebSocket() {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            this._setConnectionStatus(true);
        };

        this.ws.onclose = () => {
            this._setConnectionStatus(false);
            setTimeout(() => this._connectWebSocket(), 2500);
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this._handleServerEvent(data);
            } catch (err) {
                console.error("WebSocket メッセージ解析エラー:", err);
            }
        };
    }

    /**
     * バックエンドから届いたイベントを処理する。
     */
    _handleServerEvent(data) {
        switch (data.type) {
            case "init_state":
                if (Array.isArray(data.presets)) {
                    this.availablePresets = data.presets;
                    this._renderSkillPresets(data.presets, data.config?.active_preset_id || "");
                }
                this._applyConfigState(data.config);
                this._renderSystemMetrics(data.system_metrics);
                this._renderMemories(data.memories || []);
                this._renderTasks(data.tasks || []);
                this._renderViewers(data.viewers || []);
                this._renderWorkflows(data.workflows || []);
                this._renderResearchJobs(data.research_jobs || []);
                this.pendingApprovals = data.pending_approvals || [];
                this._renderPendingApprovals();
                this._renderKaraokeTracks(data.karaoke_tracks || []);
                this._renderPollOverlay(data.poll || null);
                this._renderInitialHistory(data.history || []);
                this._renderAppearanceState(
                    {
                        active_outfit: data.config?.active_outfit,
                        outfit_genres: data.outfit_genres,
                        outfits: data.outfits,
                        affection: data.affection,
                    },
                    false
                );
                break;

            case "appearance_updated":
                this._renderAppearanceState(data, true);
                break;

            case "model_routed":
                this._updateRouterPill(data.tier, data.model, data.reason);
                if (data.model) {
                    this._appendThoughtItem(
                        `[LLM自動ルーティング] tier=${data.tier || "auto"} -> ${data.model}${data.reason ? ` (${data.reason})` : ""}`,
                        "tool"
                    );
                }
                break;

            case "speech_ready":
                if (data.text) {
                    if (this.speechStreamQueue && this.speechStreamQueue.eventId === data.event_id) {
                        this.speechStreamQueue.fullSubtitleShown = true;
                    }
                    this._showSubtitle(data.text, data.emotion || "normal", 16000);
                }
                break;

            case "speech_chunk":
                this._enqueueSpeechChunk(data);
                break;

            case "speech_stop":
                this.stopCurrentSpeech();
                break;

            case "engine_status":
                this._renderEngineStatus(data.engine_status);
                break;

            case "discord_status":
                if (this.currentConfig) {
                    this.currentConfig.discord_status = data.discord_status;
                }
                this._renderDiscordStatus(data.discord_status);
                break;

            case "topic_updated":
                this.currentConfig.stream_topic = data.topic || "";
                this._renderTopicWidget();
                break;

            case "viewers_updated":
                this._renderViewers(data.viewers || []);
                break;

            case "client_open_url":
                if (data.url) {
                    window.open(data.url, "_blank", "noopener");
                }
                break;

            case "config_updated": {
                const prevMode = this.currentConfig ? this.currentConfig.mode : null;
                const prevActiveModel = this.currentConfig
                    ? (this.currentConfig.active_ollama_model || this.currentConfig.ollama_model)
                    : null;
                this._applyConfigState(data.config);
                if (data.memories) {
                    this._renderMemories(data.memories);
                }
                if (prevMode !== this.currentConfig.mode && Array.isArray(data.history)) {
                    this._renderInitialHistory(data.history, { showSubtitle: false, appendThoughts: false });
                }
                const nextActiveModel = this.currentConfig.active_ollama_model || this.currentConfig.ollama_model;
                if (prevActiveModel && nextActiveModel && prevActiveModel !== nextActiveModel) {
                    this._appendThoughtItem(
                        `[LLM切替] モード (${this.currentConfig.mode}) に応じてモデルを「${nextActiveModel}」へ切り替えました。`,
                        "tool"
                    );
                }
                if (!this.avatar.isSpeaking && prevMode !== this.currentConfig.mode) {
                    const def = this._getPersonaDefaultSpeech();
                    this._showSubtitle(def.speech, def.emotion);
                }
                break;
            }

            case "conversations_cleared": {
                if (!data.mode || data.mode === this.currentConfig.mode) {
                    const chatContainer = document.getElementById("chat-feed");
                    if (chatContainer) chatContainer.innerHTML = "";
                    this._hideSubtitle();
                }
                break;
            }

            case "demo_stream_toggled": {
                const prevMode = this.currentConfig ? this.currentConfig.mode : null;
                const prevActiveModel = this.currentConfig.active_ollama_model || this.currentConfig.ollama_model;
                this.currentConfig.demo_stream_active = data.demo_stream_active;
                this.currentConfig.mode = data.mode;
                if (data.active_ollama_model) {
                    this.currentConfig.active_ollama_model = data.active_ollama_model;
                }
                this._applyConfigState(this.currentConfig);
                if (prevMode !== this.currentConfig.mode && Array.isArray(data.history)) {
                    this._renderInitialHistory(data.history, { showSubtitle: false, appendThoughts: false });
                }
                const nextActiveModel = this.currentConfig.active_ollama_model || this.currentConfig.ollama_model;
                if (prevActiveModel && nextActiveModel && prevActiveModel !== nextActiveModel) {
                    this._appendThoughtItem(
                        `[LLM切替] モード (${this.currentConfig.mode}) に応じてモデルを「${nextActiveModel}」へ切り替えました。`,
                        "tool"
                    );
                }
                break;
            }

            case "system_metrics":
                this._renderSystemMetrics(data.system_metrics);
                break;

            case "pomodoro_tick":
                this._renderPomodoro(data.pomodoro);
                break;

            case "task_due_alert":
                this._playSE("alert");
                if (data.tasks) this._renderTasks(data.tasks);
                if (data.task) {
                    this._appendThoughtItem(`🔔 [予定時刻到来] 「${data.task.title}」`, "tool");
                }
                break;

            case "incoming_event":
                if (data.event.source === "superchat") {
                    this._playSE("superchat");
                    this.avatar.triggerActionEffect("celebrate", 2600);
                }
                this._appendFeedItem({
                    role: "user",
                    source: data.event.source,
                    user: data.event.user,
                    content: data.event.content,
                    metadata: data.event.metadata,
                });
                break;

            case "agent_thinking":
                this._clearEmotionReset();
                this._setAgentStatus("THINKING / 思考中...", true);
                this.avatar.isThinking = true;
                break;

            case "tool_execution":
                this._clearEmotionReset();
                this._setAgentStatus(`TOOL: ${data.tool_name}`, true);
                this.avatar.setEmotion(data.emotion || "serious");
                this._updateEmotionBadge(data.emotion || "serious");
                this._appendThoughtItem(
                    `[自律ツール実行: ${data.tool_name}] 引数: ${JSON.stringify(data.arguments)} | 思考: ${data.thought}`,
                    "tool"
                );
                break;

            case "tool_result":
                this._appendThoughtItem(
                    `[ツール観測結果: ${data.tool_name}] ${data.observation}`,
                    "observation"
                );
                break;

            case "agent_response": {
                this._clearEmotionReset();
                this.avatar.isThinking = false;
                this._setAgentStatus("ACTIVE / 待機中", false);
                this.avatar.setEmotion(data.emotion);
                this._updateEmotionBadge(data.emotion);
                if (data.routed_model || data.routing_tier) {
                    this._updateRouterPill(
                        data.routing_tier,
                        data.routed_model,
                        data.self_reviewed ? "自己レビュー済" : ""
                    );
                }
                if (data.action === "celebrate") {
                    this.avatar.triggerActionEffect("celebrate", 2400);
                }
                const speechSub = data.speech_sub || "";
                const streamedCount = Number(data.streamed_chunks_count || 0);
                if (this.speechStreamQueue) {
                    this.speechStreamQueue.fullSubtitleShown = true;
                }
                this._showSubtitle(data.speech, data.emotion, null, speechSub);
                this._appendThoughtItem(
                    `[判断根拠 (${data.emotion})${data.self_reviewed ? " / 自己レビュー済" : ""}] ${data.thought}`,
                    "thought"
                );
                this._appendFeedItem({
                    role: "agent",
                    user: this.currentConfig.agent_name || "ニア (NIA)",
                    emotion: data.emotion,
                    content: data.speech,
                    speechSub,
                    evidences: data.evidences || [],
                });
                if (data.memories) this._renderMemories(data.memories);
                if (data.tasks) this._renderTasks(data.tasks);
                if (data.viewers) this._renderViewers(data.viewers);
                if (data.rag_stats) this._renderRagStats(data.rag_stats);
                if (data.affection || data.environment) {
                    this._renderAppearanceState(
                        { affection: data.affection, environment: data.environment },
                        false
                    );
                }
                if (!data.voice_muted && !this.currentConfig.voice_muted) {
                    if (streamedCount > 0 && this.speechStreamQueue) {
                        this.speechStreamQueue.finalized = true;
                        this.speechStreamQueue.expectedCount = streamedCount;
                        if (!this.speechStreamQueue.playing) {
                            this._drainSpeechStreamQueue();
                        }
                    } else {
                        this._resetStreamQueue();
                        this._playAgentVoice(data.audio_url, data.speech, data.emotion);
                    }
                } else {
                    this._resetStreamQueue();
                    this._scheduleEmotionReset(2500);
                }
                break;
            }

            case "tasks_updated":
                this._renderTasks(data.tasks || []);
                break;

            case "memories_updated":
                this._renderMemories(data.memories || []);
                if (data.consolidation_report) {
                    const rep = data.consolidation_report;
                    this._appendThoughtItem(
                        `[夜間睡眠統合] 類似統合 ${rep.merged_count || 0}件 / 整理 ${rep.pruned_count || 0}件（${rep.before_count || 0} → ${rep.after_count || 0}件）`,
                        "tool"
                    );
                }
                break;

            case "workflows_updated":
                this._renderWorkflows(data.workflows || []);
                break;

            case "research_updated":
                this._renderResearchJobs(data.jobs || []);
                if (data.completed_job) {
                    this._appendThoughtItem(`[Deep Research完了] ${data.completed_job.report_path}`, "tool");
                }
                break;

            case "rag_updated":
                this._renderRagStats(data.stats);
                break;

            case "approval_requested":
                if (data.request) {
                    this.pendingApprovals.push(data.request);
                    this._renderPendingApprovals();
                    this._playSE("alert");
                }
                break;

            case "approval_resolved":
                if (data.summary) {
                    this.pendingApprovals = this.pendingApprovals.filter((r) => r.id !== data.summary.id);
                    this._renderPendingApprovals();
                }
                break;

            case "news_overlay":
                this._renderNewsOverlay(data.cards || []);
                break;

            case "poll_update":
                this._renderPollOverlay(data.poll || null);
                break;

            case "mailbag_overlay":
                this._renderMailbagOverlay(data.item || null);
                break;

            case "karaoke_play":
                if (data.track && this.isMainConsole) {
                    this._playKaraokeTrack(data.track);
                }
                break;

            case "stream_session_updated":
                if (this.currentConfig) {
                    this.currentConfig.active_stream_session = data.active_session || null;
                    this._renderStreamSessionBadge(data.active_session);
                }
                break;
        }
    }

    /**
     * チャットまたはコメントイベントをサーバーへ送信する。
     */
    sendChatMessage(content, source = "user_text", user = "Master", metadata = {}) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(
                JSON.stringify({
                    type: "chat",
                    source: source,
                    user: user,
                    content: content,
                    metadata: metadata,
                })
            );
        } else {
            fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content, source, user, metadata }),
            });
        }
    }

    /**
     * 設定変更をバックエンドAPIへ送信する。
     */
    async updateConfig(partialConfig) {
        const resp = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(partialConfig),
        });
        return await resp.json();
    }

    _resetManualMouthToAuto() {
        this.avatar.setManualVowel(null);
        document.querySelectorAll(".vowel-btn").forEach((b) => {
            b.classList.toggle("active", b.dataset.vowel === "auto");
        });
    }

    /**
     * 発話終了後の通常顔リセットタイマーを解除する。
     */
    _clearEmotionReset() {
        if (this.emotionResetTimer) {
            clearTimeout(this.emotionResetTimer);
            this.emotionResetTimer = null;
        }
    }

    /**
     * ブラウザの Document Picture-in-Picture API（最前面固定・アドレスバーなし）を用いて
     * フローティング常駐マスコットを起動・停止する。
     */
    async _toggleBrowserPiPMascot() {
        // 既に開いている場合は閉じる
        if (this.pipWindow && !this.pipWindow.closed) {
            this.pipWindow.close();
            this.pipWindow = null;
            await fetch("/api/mascot/toggle", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ active: false }),
            });
            return;
        }
        if (this.mascotPopup && !this.mascotPopup.closed) {
            this.mascotPopup.close();
            this.mascotPopup = null;
            await fetch("/api/mascot/toggle", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ active: false }),
            });
            return;
        }

        let opened = false;
        if ("documentPictureInPicture" in window) {
            try {
                const pipWin = await window.documentPictureInPicture.requestWindow({
                    width: 310,
                    height: 460,
                });
                pipWin.document.documentElement.style.cssText = "margin:0;padding:0;height:100%;overflow:hidden;background:#050811;";
                pipWin.document.body.style.cssText = "margin:0;padding:0;height:100%;overflow:hidden;background:#050811;";
                const iframe = pipWin.document.createElement("iframe");
                iframe.src = `${window.location.origin}/?mode=mascot`;
                iframe.allow = "microphone; autoplay; display-capture; clipboard-read; clipboard-write";
                iframe.style.cssText = "width:100%;height:100%;border:none;overflow:hidden;display:block;";
                pipWin.document.body.appendChild(iframe);
                pipWin.addEventListener("pagehide", () => {
                    this.pipWindow = null;
                    fetch("/api/mascot/toggle", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ active: false }),
                    });
                });
                this.pipWindow = pipWin;
                opened = true;
            } catch (err) {
                console.warn("Document PiP 起動フォールバック:", err);
            }
        }

        if (!opened) {
            this.mascotPopup = window.open(
                `${window.location.origin}/?mode=mascot`,
                "NIA_Desktop_Mascot",
                "width=320,height=470,menubar=no,toolbar=no,location=no,status=no"
            );
            opened = !!this.mascotPopup;
        }

        await fetch("/api/mascot/toggle", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active: opened }),
        });

        this._appendThoughtItem(
            "[常駐マスコット] 最前面フローティングPiPマスコットを起動しました（背景完全透過のWindowsネイティブ版を使う場合はフォルダ内の『NIA_起動・再起動.vbs』または『NIA_マスコット常駐のみ起動.vbs』から起動してください）。",
            "tool"
        );
    }

    /**
     * 発話終了から数秒後（デフォルト2.5秒後）に表情を「通常 (normal)」へ自動的に戻す。
     * @param {number} delayMs
     */
    _scheduleEmotionReset(delayMs = 2500) {
        this._clearEmotionReset();
        this.emotionResetTimer = setTimeout(() => {
            this.emotionResetTimer = null;
            if (!this.avatar.isSpeaking && !this.avatar.isThinking) {
                this.avatar.setEmotion("normal");
                this._updateEmotionBadge("normal");
            }
        }, delayMs);
    }

    /**
     * 現在再生中の音声（WAV/MP3・ブラウザTTS）・口パク・BGMダッキングを即座に停止する。
     */
    _resetStreamQueue() {
        this.speechStreamQueue = null;
    }

    /**
     * 先読みStreaming合成された文チャンクを受信順に並べてシームレスに連続再生する。
     */
    _enqueueSpeechChunk(chunk) {
        if (!chunk || !chunk.event_id) return;
        const evId = String(chunk.event_id);
        if (!this.speechStreamQueue || this.speechStreamQueue.eventId !== evId) {
            if (this.currentAudio) {
                try {
                    this.currentAudio.pause();
                    this.currentAudio.currentTime = 0;
                } catch (_) {}
                this.currentAudio = null;
            }
            if (this.audioMonitorFrame) {
                cancelAnimationFrame(this.audioMonitorFrame);
                this.audioMonitorFrame = null;
            }
            this.speechStreamQueue = {
                eventId: evId,
                nextSeq: 0,
                chunks: new Map(),
                playing: false,
                finalized: false,
                expectedCount: null,
                fullSubtitleShown: false,
                accumulatedText: "",
            };
        }
        const seq = Number(chunk.seq ?? 0);
        this.speechStreamQueue.chunks.set(seq, chunk);
        if (!this.speechStreamQueue.playing) {
            this._drainSpeechStreamQueue();
        }
    }

    async _drainSpeechStreamQueue() {
        const q = this.speechStreamQueue;
        if (!q || q.playing) return;
        if (this.currentConfig.voice_muted || this._shouldSkipAudioForDuplicatePrevention()) {
            this._resetStreamQueue();
            this._scheduleEmotionReset(2500);
            return;
        }
        let chunk = q.chunks.get(q.nextSeq);
        if (!chunk && q.finalized && q.chunks.size > 0) {
            // 合成失敗等で先頭側の seq が欠番になった場合でも後続チャンクが詰まらないよう最小 seq へ前進
            const minSeq = Math.min(...q.chunks.keys());
            if (Number.isFinite(minSeq)) {
                q.nextSeq = minSeq;
                chunk = q.chunks.get(minSeq);
            }
        }
        if (!chunk) {
            if (q.finalized && q.chunks.size === 0) {
                this.avatar.stopSpeech();
                if (this.isMainConsole && this.audioSyncChannel) {
                    this.audioSyncChannel.postMessage({ type: "lipsync_stop" });
                }
                this._setBgmDucking(false);
                this._updateVisualizerBars(null);
                this._scheduleEmotionReset(2500);
                this._scheduleSubtitleClear(5000);
                this._resetStreamQueue();
            }
            return;
        }

        q.chunks.delete(q.nextSeq);
        q.nextSeq += 1;
        q.playing = true;

        this._clearEmotionReset();
        this._resetManualMouthToAuto();
        if (chunk.emotion) {
            this.avatar.setEmotion(chunk.emotion);
            this._updateEmotionBadge(chunk.emotion);
        }

        try {
            if (chunk.audio_url) {
                await this._playAudioUrlInternal(chunk.audio_url, chunk.text || "", chunk.emotion || "normal", {
                    keepAliveBetweenChunks: true,
                    awaitEnd: true,
                });
            }
        } catch (err) {
            console.warn("Streaming音声チャンク再生エラー:", err);
        } finally {
            if (this.speechStreamQueue === q) {
                q.playing = false;
                this._drainSpeechStreamQueue();
            }
        }
    }

    stopCurrentSpeech() {
        this._resetStreamQueue();
        if (this.currentAudio) {
            try {
                this.currentAudio.pause();
                this.currentAudio.currentTime = 0;
            } catch (_) {}
            this.currentAudio = null;
        }
        if (this.audioMonitorFrame) {
            cancelAnimationFrame(this.audioMonitorFrame);
            this.audioMonitorFrame = null;
        }
        if ("speechSynthesis" in window) {
            try {
                window.speechSynthesis.cancel();
            } catch (_) {}
        }
        this.avatar.stopSpeech();
        if (this.isMainConsole && this.audioSyncChannel) {
            this.audioSyncChannel.postMessage({ type: "lipsync_stop" });
        }
        this._setBgmDucking(false);
        this._updateVisualizerBars(null);
        this._scheduleEmotionReset(600);
        this._scheduleSubtitleClear(1800);
    }

    /**
     * 共通音声再生ヘルパー: Web Audio API AnalyserNode + ピークリミッターで口パク＋BGMオートダッキングを行う。
     */
    async _playAudioUrlInternal(audioUrl, speechText, emotion, opts = {}) {
        const { keepAliveBetweenChunks = false, awaitEnd = false } = opts;
        const ctx = await this._ensureAudioContext();
        if (this.selectedAudioSinkId && typeof ctx.setSinkId === "function") {
            try {
                await ctx.setSinkId(this.selectedAudioSinkId);
            } catch (e) {
                console.warn("AudioContext.setSinkId 適用スキップ:", e);
            }
        }

        const audio = new Audio(audioUrl);
        audio.crossOrigin = "anonymous";
        if (this.selectedAudioSinkId && typeof audio.setSinkId === "function") {
            try {
                await audio.setSinkId(this.selectedAudioSinkId);
            } catch (_) {}
        }
        this.currentAudio = audio;

        const source = ctx.createMediaElementSource(audio);
        const limiter = ctx.createDynamicsCompressor();
        limiter.threshold.setValueAtTime(-3.0, ctx.currentTime);
        limiter.knee.setValueAtTime(6.0, ctx.currentTime);
        limiter.ratio.setValueAtTime(12.0, ctx.currentTime);
        limiter.attack.setValueAtTime(0.002, ctx.currentTime);
        limiter.release.setValueAtTime(0.08, ctx.currentTime);

        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        source.connect(limiter);
        limiter.connect(analyser);
        analyser.connect(ctx.destination);

        const dataArray = new Uint8Array(analyser.frequencyBinCount);

        const updateLipSync = () => {
            if (!this.currentAudio || this.currentAudio !== audio || audio.paused || audio.ended) {
                this.avatar.stopSpeech();
                if (!keepAliveBetweenChunks) {
                    this._setBgmDucking(false);
                }
                this._updateVisualizerBars(null);
                return;
            }
            analyser.getByteFrequencyData(dataArray);
            let sum = 0;
            let lowBand = 0;
            let midBand = 0;
            let highBand = 0;
            const count = Math.min(48, dataArray.length);
            for (let i = 2; i < count; i++) {
                const v = dataArray[i];
                sum += v;
                if (i < 10) lowBand += v;
                else if (i < 26) midBand += v;
                else highBand += v;
            }
            const avg = sum / (count - 2);
            const normalizedLevel = avg <= 15 ? 0.0 : Math.min(1.0, (avg - 15) / 110);
            let vowelHint = null;
            if (normalizedLevel > 0.25) {
                if (highBand > midBand * 0.95 && highBand > lowBand * 0.9) vowelHint = "i";
                else if (lowBand > midBand * 1.15 && midBand < 950) vowelHint = "o";
                else if (midBand > lowBand * 1.1) vowelHint = "a";
                else vowelHint = "e";
            }
            this.avatar.setAudioLevel(normalizedLevel, vowelHint);
            if (this.isMainConsole && this.audioSyncChannel) {
                this.audioSyncChannel.postMessage({ type: "lipsync_level", level: normalizedLevel });
            }
            this._updateVisualizerBars(normalizedLevel > 0.02 ? dataArray : null);
            this.audioMonitorFrame = requestAnimationFrame(updateLipSync);
        };

        return new Promise(async (resolve, reject) => {
            let settled = false;
            const finishPlayback = () => {
                if (settled) return;
                settled = true;
                if (this.currentAudio === audio) {
                    this.currentAudio = null;
                }
                if (this.audioMonitorFrame) {
                    cancelAnimationFrame(this.audioMonitorFrame);
                    this.audioMonitorFrame = null;
                }
                if (!keepAliveBetweenChunks) {
                    this.avatar.stopSpeech();
                    if (this.isMainConsole && this.audioSyncChannel) {
                        this.audioSyncChannel.postMessage({ type: "lipsync_stop" });
                    }
                    this._setBgmDucking(false);
                    this._updateVisualizerBars(null);
                    this._scheduleEmotionReset(2500);
                    this._scheduleSubtitleClear(5000);
                }
                resolve();
            };

            audio.onplay = () => {
                this._clearEmotionReset();
                this._clearSubtitleTimer();
                this._setBgmDucking(true);
                this.avatar.startSpeech(speechText, "webaudio");
                if (this.isMainConsole && this.audioSyncChannel) {
                    this.audioSyncChannel.postMessage({ type: "lipsync_start", speechText });
                }
                updateLipSync();
            };
            audio.onended = finishPlayback;
            audio.onerror = finishPlayback;

            try {
                await audio.play();
                if (!awaitEnd) {
                    resolve();
                }
            } catch (err) {
                if (!settled) {
                    settled = true;
                }
                reject(err);
            }
        });
    }

    /**
     * 音声ファイルを再生し、Web Audio API AnalyserNode で音量をリアルタイム計測して口パク＋BGMオートダッキングさせる。
     * AivisSpeech 等のピーク音割れを防ぐため DynamicsCompressorNode によるピークリミッターを通過させる。
     */
    async _playAgentVoice(audioUrl, speechText, emotion) {
        this._clearEmotionReset();
        this._resetManualMouthToAuto();

        if (this.currentAudio) {
            this.currentAudio.pause();
            this.currentAudio = null;
        }
        if (this.audioMonitorFrame) {
            cancelAnimationFrame(this.audioMonitorFrame);
            this.audioMonitorFrame = null;
        }
        this.avatar.stopSpeech();
        if (this.isMainConsole && this.audioSyncChannel) {
            this.audioSyncChannel.postMessage({ type: "lipsync_stop" });
        }
        this._setBgmDucking(false);

        if (this.currentConfig.voice_muted || this._shouldSkipAudioForDuplicatePrevention()) {
            this._scheduleEmotionReset(2500);
            return;
        }

        if (audioUrl) {
            try {
                await this._playAudioUrlInternal(audioUrl, speechText, emotion, {
                    keepAliveBetweenChunks: false,
                    awaitEnd: false,
                });
                return;
            } catch (err) {
                console.warn("Audio再生フォールバック:", err);
            }
        }

        this._speakWithBrowserTTS(speechText, emotion);
    }

    _speakWithBrowserTTS(text, emotion) {
        if (!("speechSynthesis" in window)) {
            this._scheduleEmotionReset(2500);
            this._scheduleSubtitleClear(5000);
            return;
        }
        window.speechSynthesis.cancel();

        this._clearEmotionReset();
        this._resetManualMouthToAuto();

        const utter = new SpeechSynthesisUtterance(text);
        utter.lang = "ja-JP";
        utter.rate = (this.currentConfig.tts_speed || 1.05) * (emotion === "joy" ? 1.08 : 1.0);
        utter.pitch = (this.currentConfig.tts_pitch || 1.0) * (emotion === "serious" ? 0.95 : 1.1);
        utter.volume = Math.min(1.0, this.currentConfig.tts_volume || 1.0);

        utter.onstart = () => {
            this._clearEmotionReset();
            this._clearSubtitleTimer();
            this._setBgmDucking(true);
            this.avatar.startSpeech(text, "browser");
        };
        utter.onend = () => {
            this.avatar.stopSpeech();
            this._setBgmDucking(false);
            this._scheduleEmotionReset(2500);
            this._scheduleSubtitleClear(5000);
        };
        utter.onerror = () => {
            this.avatar.stopSpeech();
            this._setBgmDucking(false);
            this._scheduleEmotionReset(2500);
            this._scheduleSubtitleClear(5000);
        };
        window.speechSynthesis.speak(utter);
    }

    /**
     * 現在のペルソナ設定（ツンデレ秘書 / VTuber / カスタム）に合致した待機・テスト用セリフと表情を返す。
     */
    _getPersonaDefaultSpeech() {
        const cfg = this.currentConfig || {};
        if (cfg.mode === "vtuber") {
            return {
                speech: "みんな聞こえてるー？ ニアの配信始まるわよ！ 変なコメントしたら即ツッコむから覚悟しなさいよね！",
                emotion: "smile",
            };
        }
        const pa = String(cfg.persona_agent || "");
        if (pa.includes("ツンデレ") || pa.includes("勘違いしないで") || pa.includes("辛辣") || pa.includes("仕方な")) {
            return {
                speech: "……べ、別にあなたのために待機してたわけじゃないわよ。システムは正常……用があるなら早く言いなさいよね。",
                emotion: "serious",
            };
        }
        // ペルソナ文中に「...」の長いセリフ例があればそれを利用
        const matches = pa.match(/「([^」]{15,90})」/g);
        if (matches && matches.length > 0) {
            const cleaned = matches[0].slice(1, -1).replace("〇〇", "すべて正常");
            return { speech: cleaned, emotion: "normal" };
        }
        return {
            speech: "システム稼働中。用件があるならいつでも指示を出してちょうだい。",
            emotion: "normal",
        };
    }

    triggerLocalVoiceDemo() {
        const def = this._getPersonaDefaultSpeech();
        this.avatar.setEmotion(def.emotion);
        this._updateEmotionBadge(def.emotion);
        this._showSubtitle(def.speech, def.emotion);
        this._speakWithBrowserTTS(def.speech, def.emotion);
    }

    /**
     * Web Speech API によるマイク音声認識（ウェイクワード判定対応）を初期化する。
     */
    _initSpeechRecognition() {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) return;

        const rec = new SpeechRecognition();
        rec.lang = "ja-JP";
        rec.interimResults = false;
        rec.continuous = false;

        rec.onresult = (event) => {
            const transcript = event.results[0][0].transcript.trim();
            if (!transcript) return;
            if (this.currentConfig.wake_word_required) {
                const ww = (this.currentConfig.wake_word || "ニア").trim();
                if (ww && !transcript.includes(ww) && !transcript.toLowerCase().includes("nia")) {
                    this._appendThoughtItem(
                        `[音声認識スキップ] ウェイクワード「${ww}」が含まれていません: "${transcript}"`,
                        "thought"
                    );
                    return;
                }
            }
            this.sendChatMessage(transcript, "user_voice", "Master (Voice)");
        };

        rec.onend = () => {
            if (this.isListeningMic) {
                try {
                    rec.start();
                } catch (_) {
                    this.isListeningMic = false;
                    this._updateMicButtonUI();
                }
            } else {
                this._updateMicButtonUI();
            }
        };

        this.recognition = rec;
    }

    toggleMicListening() {
        if (!this.recognition) {
            alert("お使いのブラウザはWeb Speech API音声認識に対応していません（Chrome / Edge推奨）。");
            return;
        }
        this.isListeningMic = !this.isListeningMic;
        if (this.isListeningMic) {
            try {
                this.recognition.start();
            } catch (_) {}
        } else {
            this.recognition.stop();
        }
        this._updateMicButtonUI();
    }

    _updateMicButtonUI() {
        const btn = document.getElementById("btn-mic");
        if (!btn) return;
        if (this.isListeningMic) {
            btn.classList.add("active");
            btn.textContent = "🎙️ 音声待受中 (ON)";
        } else {
            btn.classList.remove("active");
            btn.textContent = "🎙️ マイク入力";
        }
    }

    /**
     * サーバー設定状態をUIコントロールおよびアバター演出に反映する。
     */
    _applyConfigState(cfg) {
        if (!cfg) return;
        const normalizedMode = cfg.mode === "hayato" ? "agent" : (cfg.mode || "agent");
        cfg.mode = normalizedMode;
        this.currentConfig = cfg;

        document.body.classList.toggle("mode-vtuber", normalizedMode === "vtuber");
        document.body.classList.toggle("mode-agent", normalizedMode !== "vtuber");

        document.querySelectorAll(".mode-btn").forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.mode === normalizedMode);
        });

        const modeIndicator = document.getElementById("current-mode-badge");
        if (modeIndicator) {
            modeIndicator.textContent =
                normalizedMode === "vtuber"
                    ? "LIVE VTUBER MODE (配信自律稼働)"
                    : "AI AGENT MODE (自律エージェント稼働)";
            modeIndicator.className = `mode-badge ${normalizedMode}`;
        }

        const speakerTag = document.getElementById("subtitle-speaker-tag");
        if (speakerTag && cfg.agent_name) {
            speakerTag.textContent = `${cfg.agent_name} // ${normalizedMode === "vtuber" ? "AI VTUBER" : "AI AGENT"}`;
        }

        // OBSモーション強度・字幕スケールの反映
        this.avatar.setMotionIntensity(cfg.obs_motion_intensity ?? 1.0);
        const subBox = document.getElementById("subtitle-box");
        if (subBox) {
            const scale = cfg.obs_subtitle_scale || 1.0;
            subBox.style.setProperty("--subtitle-scale", String(scale));
        }

        // 常駐マスコット起動状態ボタン
        const mascotBtn = document.getElementById("btn-toggle-mascot");
        if (mascotBtn) {
            mascotBtn.classList.toggle("active", !!cfg.mascot_running);
            mascotBtn.textContent = cfg.mascot_running
                ? "🐾 常駐マスコット稼働中 (停止)"
                : "🐾 常駐マスコット起動";
        }

        // 実配信接続バッジ
        const liveBadge = document.getElementById("live-conn-badge");
        if (liveBadge) {
            const hasYt = !!(cfg.youtube_live_id && cfg.youtube_live_id.trim());
            const hasTw = !!(cfg.twitch_channel && cfg.twitch_channel.trim());
            if (hasYt && hasTw) {
                liveBadge.textContent = "🔴YT + 🟣TWITCH LIVE";
                liveBadge.className = "live-conn-badge connected";
            } else if (hasYt) {
                liveBadge.textContent = "🔴 YOUTUBE LIVE 接続中";
                liveBadge.className = "live-conn-badge connected";
            } else if (hasTw) {
                liveBadge.textContent = `🟣 TWITCH (#${cfg.twitch_channel}) 接続中`;
                liveBadge.className = "live-conn-badge connected";
            } else {
                liveBadge.textContent = "LOCAL SIM";
                liveBadge.className = "live-conn-badge";
            }
        }

        // ポモドーロ状態反映
        if (cfg.pomodoro) {
            this._renderPomodoro(cfg.pomodoro);
        }

        // ミュートボタン・配信テーマ・エンジンステータス反映
        const muteBtn = document.getElementById("btn-toggle-mute");
        if (muteBtn) {
            const muted = !!cfg.voice_muted;
            muteBtn.classList.toggle("muted", muted);
            muteBtn.textContent = muted ? "🔇 ミュート中" : "🔊 音声ON";
        }
        const topicInput = document.getElementById("input-stream-topic");
        if (topicInput && document.activeElement !== topicInput && cfg.stream_topic !== undefined) {
            topicInput.value = cfg.stream_topic || "";
        }
        if (cfg.engine_status) {
            this._renderEngineStatus(cfg.engine_status);
        }
        if (cfg.discord_status) {
            this._renderDiscordStatus(cfg.discord_status);
        }
        if (cfg.rag_stats) {
            this._renderRagStats(cfg.rag_stats);
        }
        this._renderStreamSessionBadge(cfg.active_stream_session || null);
        const bgmSceneSel = document.getElementById("select-bgm-scene");
        if (bgmSceneSel && cfg.obs_bgm_scene) {
            bgmSceneSel.value = cfg.obs_bgm_scene;
        }
        this._renderTopicWidget();

        const modelSelect = document.getElementById("select-ollama-model");
        if (modelSelect && Array.isArray(cfg.available_ollama_models)) {
            const activeModel =
                cfg.active_ollama_model ||
                (normalizedMode === "vtuber" ? (cfg.ollama_model_vtuber || "gemma4:12b") : cfg.ollama_model);
            modelSelect.innerHTML = "";
            for (const m of cfg.available_ollama_models) {
                const opt = document.createElement("option");
                opt.value = m;
                opt.textContent = m;
                if (m === activeModel) opt.selected = true;
                modelSelect.appendChild(opt);
            }
        }

        const ttsSelect = document.getElementById("select-tts-provider");
        if (ttsSelect && cfg.tts_provider) {
            ttsSelect.value = cfg.tts_provider;
        }

        const proactiveCheck = document.getElementById("check-proactive");
        if (proactiveCheck && typeof cfg.proactive_enabled === "boolean") {
            proactiveCheck.checked = cfg.proactive_enabled;
        }

        const discordAutonomousCheck = document.getElementById("check-discord-autonomous");
        if (discordAutonomousCheck && typeof cfg.discord_notify_autonomous === "boolean") {
            discordAutonomousCheck.checked = cfg.discord_notify_autonomous;
        }
        const discordAutonomousLabel = document.getElementById("label-discord-autonomous");
        if (discordAutonomousLabel) {
            const dimmed = cfg.proactive_enabled === false || cfg.discord_enabled === false;
            discordAutonomousLabel.classList.toggle("is-dimmed", dimmed);
        }

        const demoBtn = document.getElementById("btn-demo-stream");
        if (demoBtn) {
            demoBtn.classList.toggle("active", !!cfg.demo_stream_active);
            demoBtn.textContent = cfg.demo_stream_active
                ? "🟢 模擬コメント自動流入: ON"
                : "⚪ 模擬コメント自動流入: OFF";
        }

        // 第2世代5大機能: UI整理モード / スキルプリセット / バイリンガル字幕 / 自動ルーター表示
        const isSimpleUi = cfg.ui_view_level === "simple";
        document.body.classList.toggle("ui-simple", isSimpleUi);
        const uiLevelBtn = document.getElementById("btn-toggle-ui-level");
        if (uiLevelBtn) {
            uiLevelBtn.classList.toggle("active", isSimpleUi);
            uiLevelBtn.textContent = isSimpleUi ? "🌱 シンプルUI" : "⚙️ フル管制";
        }

        const biBtn = document.getElementById("btn-toggle-bilingual");
        if (biBtn) {
            const biOn = !!cfg.bilingual_enabled;
            const langCode = String(cfg.bilingual_target_lang || "en").toUpperCase();
            biBtn.classList.toggle("active", biOn);
            biBtn.textContent = biOn ? `🌐 字幕:${langCode}` : "🌐 字幕:OFF";
        }

        const presetSel = document.getElementById("select-skill-preset");
        if (presetSel) {
            presetSel.value = cfg.active_preset_id || "";
        }

        const ollamaDatalist = document.getElementById("ollama-models-datalist");
        if (ollamaDatalist && Array.isArray(cfg.available_ollama_models) && cfg.available_ollama_models.length > 0) {
            ollamaDatalist.innerHTML = "";
            for (const m of cfg.available_ollama_models) {
                const opt = document.createElement("option");
                opt.value = m;
                ollamaDatalist.appendChild(opt);
            }
        }

        const routingMode = cfg.llm_routing_mode || "auto";
        const activeM =
            cfg.active_ollama_model ||
            (normalizedMode === "vtuber" ? cfg.ollama_model_vtuber : cfg.ollama_model) ||
            "";
        if (routingMode === "off" || routingMode === "manual") {
            this._updateRouterPill("manual", activeM, "手動固定");
        } else {
            this._updateRouterPill(routingMode === "auto" ? "balanced" : routingMode, activeM, "待機");
        }
        this.avatar.interactionEnabled = cfg.interaction_enabled !== false;
        this.avatar.setEnvironment(null, cfg.weather_bg_effect_enabled !== false);
    }

    async _sendAvatarInteraction(kind) {
        try {
            const res = await fetch("/api/interact", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind }),
            }).then((r) => r.json());
            if (res && res.ok && res.affection) {
                this._renderAppearanceState({ affection: res.affection }, false);
                if (res.affection.leveled_up) {
                    this.avatar.triggerActionEffect("celebrate", 2600);
                    this._playSE("superchat");
                    this._appendThoughtItem(
                        `[親密度アップ!] Lv.${res.affection.level}「${res.affection.title}」に到達しました！`,
                        "tool"
                    );
                }
            }
        } catch (e) {
            console.warn("インタラクション送信失敗:", e);
        }
    }

    _setupOutfitUploadModal() {
        const modal = document.getElementById("outfit-upload-modal");
        const openBtn = document.getElementById("btn-open-outfit-modal");
        const closeBtn = document.getElementById("btn-close-outfit-modal");
        const cancelBtn = document.getElementById("btn-cancel-outfit-modal");
        const submitBtn = document.getElementById("btn-submit-outfit-upload");
        const copyBtn = document.getElementById("btn-copy-nbp-prompt");
        const descInput = document.getElementById("input-nbp-outfit-desc");
        const promptArea = document.getElementById("textarea-nbp-prompt");
        const fullFile = document.getElementById("input-outfit-full-file");
        const bustFile = document.getElementById("input-outfit-bust-file");
        const fullZone = document.getElementById("dropzone-outfit-full");
        const bustZone = document.getElementById("dropzone-outfit-bust");
        const fullPrev = document.getElementById("preview-outfit-full");
        const bustPrev = document.getElementById("preview-outfit-bust");
        const genreInput = document.getElementById("input-outfit-genre");
        const nameInput = document.getElementById("input-outfit-name");
        const idInput = document.getElementById("input-outfit-id");
        const flipInput = document.getElementById("input-outfit-flip");
        const faceSyncInput = document.getElementById("input-outfit-face-sync");
        const statusEl = document.getElementById("outfit-upload-status");
        const tabAddEl = document.getElementById("outfit-tab-add");
        const tabManageEl = document.getElementById("outfit-tab-manage");
        if (!modal || !openBtn) return;

        this._pendingOutfitFullB64 = "";
        this._pendingOutfitBustB64 = "";

        const switchTab = (tabName) => {
            modal.querySelectorAll(".outfit-tab-btn").forEach((b) => {
                b.classList.toggle("active", b.dataset.outfitTab === tabName);
            });
            if (tabAddEl) tabAddEl.classList.toggle("hidden", tabName !== "add");
            if (tabManageEl) tabManageEl.classList.toggle("hidden", tabName !== "manage");
            if (submitBtn) submitBtn.classList.toggle("hidden", tabName !== "add");
            if (tabName === "manage") {
                this._renderOutfitManageGrid();
            }
        };
        modal.querySelectorAll(".outfit-tab-btn").forEach((b) => {
            b.addEventListener("click", () => switchTab(b.dataset.outfitTab || "add"));
        });

        const buildPrompt = (outfitDesc) => {
            const desc = (outfitDesc || "").trim() || "赤いベルベットのサンタミニワンピース（白いふわふわファー縁取り・黒ベルト・ミニサンタ帽・白ニーハイ・赤ブーツ）";
            return [
                "添付したキャラクター立ち絵の人物（顔・表情・赤い瞳・褐色肌・黒髪ツインテール・体格・立ちポーズ・画像内の位置とサイズ）は一切変更せずそのまま維持し、服装だけ以下のように変更してください。",
                `変更後の服装：${desc}`,
                "背景は完全に均一な純緑クロマキーグリーン（#00FF00）一色にし、足元の影・ドロップシャドウ・キャラへの緑色の照り返し・文字・枠・小物は一切入れないでください。",
            ].join("\n");
        };

        const syncPrompt = () => {
            if (promptArea) promptArea.value = buildPrompt(descInput?.value || "");
        };
        if (descInput) {
            descInput.addEventListener("input", syncPrompt);
            if (!descInput.value) {
                descInput.value = "赤いベルベットのサンタミニワンピース（白いふわふわファー縁取り・黒ベルト・ミニサンタ帽・白ニーハイ・赤ブーツ）";
            }
        }
        syncPrompt();

        modal.querySelectorAll(".nbp-chip").forEach((chip) => {
            chip.addEventListener("click", () => {
                if (descInput) descInput.value = chip.dataset.outfit || "";
                if (nameInput && chip.dataset.name) nameInput.value = chip.dataset.name;
                if (genreInput && chip.dataset.genre) genreInput.value = chip.dataset.genre;
                syncPrompt();
            });
        });

        if (copyBtn && promptArea) {
            copyBtn.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(promptArea.value);
                    const prev = copyBtn.textContent;
                    copyBtn.textContent = "✅ コピー完了！";
                    setTimeout(() => { copyBtn.textContent = prev; }, 1600);
                } catch (_) {
                    promptArea.select();
                    document.execCommand("copy");
                }
            });
        }

        const bindImageDrop = (fileInput, zoneEl, previewEl, targetKey, defaultHtml) => {
            const loadFile = (file) => {
                if (!file || !file.type.startsWith("image/")) return;
                const reader = new FileReader();
                reader.onload = () => {
                    const dataUrl = String(reader.result || "");
                    this[targetKey] = dataUrl;
                    if (previewEl) {
                        previewEl.innerHTML = `<img src="${dataUrl}" alt="preview" />`;
                    }
                };
                reader.readAsDataURL(file);
            };
            if (fileInput) {
                fileInput.addEventListener("change", () => loadFile(fileInput.files?.[0]));
            }
            if (zoneEl) {
                zoneEl.addEventListener("dragover", (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    zoneEl.classList.add("dragover");
                });
                zoneEl.addEventListener("dragleave", () => zoneEl.classList.remove("dragover"));
                zoneEl.addEventListener("drop", (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    zoneEl.classList.remove("dragover");
                    loadFile(e.dataTransfer?.files?.[0]);
                });
            }
            return () => {
                this[targetKey] = "";
                if (fileInput) fileInput.value = "";
                if (previewEl) previewEl.innerHTML = defaultHtml;
            };
        };

        const resetFull = bindImageDrop(
            fullFile,
            fullZone,
            fullPrev,
            "_pendingOutfitFullB64",
            "<span>🖼️ 全身画像をドロップ<br>またはクリック選択（必須）</span>"
        );
        const resetBust = bindImageDrop(
            bustFile,
            bustZone,
            bustPrev,
            "_pendingOutfitBustB64",
            "<span>🙂 バストアップ専用画像<br>（任意・2K以上の全身なら不要）</span>"
        );

        const closeModal = () => modal.classList.add("hidden");
        openBtn.addEventListener("click", () => {
            if (statusEl) {
                statusEl.textContent = "";
                statusEl.classList.remove("error");
            }
            syncPrompt();
            modal.classList.remove("hidden");
        });
        if (closeBtn) closeBtn.addEventListener("click", closeModal);
        if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (e) => {
            if (e.target === modal) closeModal();
        });

        if (submitBtn) {
            submitBtn.addEventListener("click", async () => {
                if (!this._pendingOutfitFullB64) {
                    if (statusEl) {
                        statusEl.textContent = "⚠️ 全身画像を選択またはドロップしてください。";
                        statusEl.classList.add("error");
                    }
                    return;
                }
                submitBtn.disabled = true;
                const origLabel = submitBtn.textContent;
                submitBtn.textContent = "⏳ 自動透過＆登録中...";
                if (statusEl) {
                    statusEl.textContent = "背景を自動判別（クロマキー緑 / AI透過）して処理しています...";
                    statusEl.classList.remove("error");
                }
                try {
                    const resp = await fetch("/api/outfits/upload", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            full_image_base64: this._pendingOutfitFullB64,
                            bust_image_base64: this._pendingOutfitBustB64 || "",
                            genre: genreInput?.value || "custom",
                            name: nameInput?.value?.trim() || "",
                            outfit_id: idInput?.value?.trim() || "",
                            flip_horizontal: !!flipInput?.checked,
                            face_sync: faceSyncInput ? !!faceSyncInput.checked : true,
                        }),
                    });
                    const res = await resp.json();
                    if (!res.ok) {
                        if (statusEl) {
                            statusEl.textContent = `❌ ${res.error || "登録に失敗しました"}`;
                            statusEl.classList.add("error");
                        }
                        return;
                    }
                    const modeLabel =
                        res.bg_mode === "chroma_green"
                            ? "クロマキー緑・スピル完全除去"
                            : res.bg_mode === "pre_transparent"
                            ? "透過PNGそのまま反映"
                            : "AI透過 (isnet-anime)";
                    const syncLabel = res.face_sync ? " / 🗣️口パク・瞬き連動ON" : "";
                    this._renderAppearanceState(res, true);
                    if (res.outfit_id) {
                        this.avatar.setOutfit(res.outfit_id, true, true, !!res.face_sync);
                    }
                    if (statusEl) {
                        statusEl.textContent = `✅ 登録完了！（${modeLabel}${syncLabel}）`;
                        statusEl.classList.remove("error");
                    }
                    resetFull();
                    resetBust();
                    setTimeout(closeModal, 900);
                } catch (err) {
                    if (statusEl) {
                        statusEl.textContent = `❌ 通信エラー: ${err}`;
                        statusEl.classList.add("error");
                    }
                } finally {
                    submitBtn.disabled = false;
                    submitBtn.textContent = origLabel;
                }
            });
        }
    }

    _renderOutfitManageGrid() {
        const grid = document.getElementById("outfit-manage-grid");
        const statusEl = document.getElementById("outfit-manage-status");
        if (!grid) return;
        const avail = (this.availableOutfits || []).filter((o) => o.available !== false);
        const genres = Array.isArray(this.outfitGenres) && this.outfitGenres.length > 0
            ? this.outfitGenres
            : [
                { id: "school", name: "🏫 制服・標準" },
                { id: "casual", name: "🧶 カジュアル・部屋着" },
                { id: "sport", name: "🏃 体操服・スポーツ" },
                { id: "swimwear", name: "🏖️ 水着・リゾート" },
                { id: "cosplay", name: "✨ コスプレ・和装" },
                { id: "custom", name: "📁 カスタム追加" },
            ];
        const curOutfit = this.currentConfig.active_outfit || "default";
        grid.innerHTML = "";

        for (const o of avail) {
            const card = document.createElement("div");
            card.className = `outfit-manage-card ${o.id === curOutfit ? "active" : ""}`;

            const thumb = document.createElement("div");
            thumb.className = "outfit-manage-thumb";
            const img = document.createElement("img");
            img.src =
                o.id === "default"
                    ? "/assets/character/normal_open_normal.png"
                    : `/assets/outfits/${encodeURIComponent(o.id)}/bust.png?t=${Date.now()}`;
            img.alt = o.name || o.id;
            thumb.appendChild(img);
            card.appendChild(thumb);

            const isDefault = o.id === "default";
            const nameInp = document.createElement("input");
            nameInp.type = "text";
            nameInp.value = o.name || o.short || o.id;
            nameInp.disabled = isDefault;
            nameInp.title = `ID: ${o.id}`;
            card.appendChild(nameInp);

            const genreSel = document.createElement("select");
            genreSel.disabled = isDefault;
            for (const g of genres) {
                const opt = document.createElement("option");
                opt.value = g.id;
                opt.textContent = g.name;
                if ((o.genre || "custom") === g.id) opt.selected = true;
                genreSel.appendChild(opt);
            }
            card.appendChild(genreSel);

            const syncLbl = document.createElement("label");
            syncLbl.className = "checkbox-row compact";
            const syncChk = document.createElement("input");
            syncChk.type = "checkbox";
            syncChk.checked = isDefault ? true : !!o.face_sync;
            syncChk.disabled = isDefault;
            const syncTxt = document.createElement("span");
            syncTxt.textContent = "🗣️ 口パク・瞬き連動";
            syncLbl.appendChild(syncChk);
            syncLbl.appendChild(syncTxt);
            card.appendChild(syncLbl);

            const act = document.createElement("div");
            act.className = "outfit-manage-actions";

            const wearBtn = document.createElement("button");
            wearBtn.type = "button";
            wearBtn.textContent = "👕 着る";
            wearBtn.addEventListener("click", async () => {
                await fetch("/api/appearance", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ outfit: o.id }),
                });
                this._renderOutfitManageGrid();
            });
            act.appendChild(wearBtn);

            if (!isDefault) {
                const saveBtn = document.createElement("button");
                saveBtn.type = "button";
                saveBtn.className = "primary";
                saveBtn.textContent = "💾 保存";
                saveBtn.addEventListener("click", async () => {
                    const resp = await fetch(`/api/outfits/${encodeURIComponent(o.id)}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            name: nameInp.value.trim(),
                            genre: genreSel.value,
                            face_sync: syncChk.checked,
                        }),
                    });
                    const res = await resp.json();
                    if (res.ok) {
                        this._renderAppearanceState(res, false);
                        if (statusEl) statusEl.textContent = `✅ 「${nameInp.value.trim()}」の設定を保存しました。`;
                        this._renderOutfitManageGrid();
                    }
                });
                act.appendChild(saveBtn);

                const delBtn = document.createElement("button");
                delBtn.type = "button";
                delBtn.className = "danger";
                delBtn.textContent = "🗑️";
                delBtn.title = "この衣装を削除";
                delBtn.addEventListener("click", async () => {
                    if (!confirm(`衣装「${o.name || o.id}」を削除しますか？`)) return;
                    const resp = await fetch(`/api/outfits/${encodeURIComponent(o.id)}`, { method: "DELETE" });
                    const res = await resp.json();
                    if (res.ok) {
                        this._renderAppearanceState(res, false);
                        if (statusEl) statusEl.textContent = `🗑️ 「${o.name || o.id}」を削除しました。`;
                        this._renderOutfitManageGrid();
                    }
                });
                act.appendChild(delBtn);
            }
            card.appendChild(act);
            grid.appendChild(card);
        }
    }

    _populateOutfitSelect(genreId, activeOutfitId) {
        const outfitSel = document.getElementById("select-outfit-item");
        if (!outfitSel) return;
        const list = (this.availableOutfits || []).filter(
            (o) => o.available !== false && (o.genre || "custom") === genreId
        );
        outfitSel.innerHTML = "";
        for (const o of list) {
            const opt = document.createElement("option");
            opt.value = o.id;
            opt.textContent = o.name || o.short || o.id;
            opt.title = o.description || o.name || "";
            outfitSel.appendChild(opt);
        }
        if (list.some((o) => o.id === activeOutfitId)) {
            outfitSel.value = activeOutfitId;
        } else if (list.length > 0) {
            outfitSel.value = list[0].id;
        }
    }

    _renderAppearanceState(state, withSparkle = false) {
        if (!state) return;
        if (Array.isArray(state.outfit_genres) && state.outfit_genres.length > 0) {
            this.outfitGenres = state.outfit_genres;
        }
        if (Array.isArray(state.outfits) && state.outfits.length > 0) {
            this.availableOutfits = state.outfits;
        }
        if (state.active_outfit !== undefined) {
            this.currentConfig.active_outfit = state.active_outfit;
            const activeMeta = (this.availableOutfits || []).find((o) => o.id === state.active_outfit);
            this.avatar.setOutfit(
                state.active_outfit,
                withSparkle,
                false,
                activeMeta ? !!activeMeta.face_sync : null
            );
        }
        if (state.affection) {
            const aff = state.affection;
            const txt = document.getElementById("affection-text");
            const fill = document.getElementById("affection-fill");
            const pill = document.getElementById("affection-pill");
            if (txt) txt.textContent = `❤️ Lv.${aff.level} ${aff.title} (${aff.points}/${aff.max_points || 100})`;
            if (fill) fill.style.width = `${Math.max(4, Math.min(100, Number(aff.progress_pct) || 0))}%`;
            if (pill) {
                pill.title = `本日のデレ度（日付変更でLv.1ツンへ自動リセット・${aff.decay_interval_min || 30}分放置ごとに自然減衰） / 累計: なでなで${aff.pet_count || 0}回・つつき${aff.poke_count || 0}回・会話${aff.chat_count || 0}回`;
            }
        }

        // ジャンルプルダウン → 衣装プルダウン描画
        const genreSel = document.getElementById("select-outfit-genre");
        if (genreSel && (this.availableOutfits || []).length > 0) {
            const avail = this.availableOutfits.filter((o) => o.available !== false);
            const curOutfit = this.currentConfig.active_outfit || "default";
            const curMeta = avail.find((o) => o.id === curOutfit) || avail[0];
            const activeGenre = curMeta ? (curMeta.genre || "school") : "school";

            const genreCatalog = Array.isArray(this.outfitGenres) && this.outfitGenres.length > 0
                ? this.outfitGenres
                : [
                    { id: "school", name: "🏫 制服・標準" },
                    { id: "casual", name: "🧶 カジュアル・部屋着" },
                    { id: "sport", name: "🏃 体操服・スポーツ" },
                    { id: "swimwear", name: "🏖️ 水着・リゾート" },
                    { id: "cosplay", name: "✨ コスプレ・和装" },
                    { id: "custom", name: "📁 カスタム追加" },
                ];
            const usedGenres = genreCatalog.filter((g) => avail.some((o) => (o.genre || "custom") === g.id));

            genreSel.innerHTML = "";
            for (const g of usedGenres) {
                const cnt = avail.filter((o) => (o.genre || "custom") === g.id).length;
                const opt = document.createElement("option");
                opt.value = g.id;
                opt.textContent = `${g.name} (${cnt})`;
                genreSel.appendChild(opt);
            }
            this.selectedOutfitGenre = activeGenre;
            genreSel.value = activeGenre;
            this._populateOutfitSelect(activeGenre, curOutfit);
        }
    }

    _renderSkillPresets(presets, activeId = "") {
        const sel = document.getElementById("select-skill-preset");
        if (!sel || !Array.isArray(presets)) return;
        sel.innerHTML = `<option value="">✨ プリセット選択...</option>`;
        for (const p of presets) {
            const opt = document.createElement("option");
            opt.value = p.id;
            opt.textContent = p.name;
            if (p.description) opt.title = p.description;
            if (activeId && p.id === activeId) opt.selected = true;
            sel.appendChild(opt);
        }
    }

    _updateRouterPill(tier, model, reason = "") {
        const pill = document.getElementById("router-status-pill");
        if (!pill) return;
        const iconMap = {
            fast: "⚡",
            balanced: "⚖️",
            smart: "🧠",
            manual: "🔒",
            auto: "🧭",
        };
        const t = (tier || "balanced").toLowerCase();
        const icon = iconMap[t] || "🧭";
        const shortModel = String(model || "").split("/").pop();
        pill.textContent = `${icon} ${t.toUpperCase()}${shortModel ? `:${shortModel}` : ""}`;
        pill.className = `router-pill tier-${t}`;
        pill.title = `LLM自動ルーティング: tier=${t}, model=${model || "--"}${reason ? ` (${reason})` : ""}`;
    }

    _renderPomodoro(pomo) {
        if (!pomo) return;
        const badge = document.getElementById("pomo-badge");
        const toggleBtn = document.getElementById("btn-pomo-toggle");
        const sec = Math.max(0, Math.floor(pomo.remaining_sec || 0));
        const mm = String(Math.floor(sec / 60)).padStart(2, "0");
        const ss = String(sec % 60).padStart(2, "0");
        if (badge) {
            const label = pomo.phase === "work" ? "🍅 作業" : "☕ 休憩";
            badge.textContent = `${label} ${mm}:${ss}`;
            badge.className = `pomo-phase ${pomo.phase}`;
        }
        if (toggleBtn) {
            toggleBtn.textContent = pomo.active ? "⏸ 一時停止" : "▶ 開始";
        }
        this._renderTopicWidget();
    }

    async _refreshStartupStatus() {
        try {
            const res = await fetch("/api/startup/status").then((r) => r.json());
            this._renderStartupStatus(res);
        } catch (_) {}
    }

    _renderStartupStatus(res) {
        const lbl = document.getElementById("startup-status-label");
        const btn = document.getElementById("btn-toggle-startup");
        if (!lbl || !res) return;
        const enabled = !!res.enabled;
        lbl.textContent = enabled ? "🟢 登録済み (自動起動ON)" : "⚪ 未登録 (手動起動)";
        if (btn) {
            btn.textContent = enabled ? "自動起動を解除" : "自動起動を登録";
        }
    }

    async _populateAudioSinks() {
        const sel = document.getElementById("cfg-audio-sink");
        if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const outputs = devices.filter((d) => d.kind === "audiooutput");
            sel.innerHTML = `<option value="">既定のスピーカー / ヘッドホン</option>`;
            outputs.forEach((d, idx) => {
                const opt = document.createElement("option");
                opt.value = d.deviceId;
                opt.textContent = d.label || `音声出力デバイス ${idx + 1}`;
                if (d.deviceId === this.selectedAudioSinkId) opt.selected = true;
                sel.appendChild(opt);
            });
        } catch (err) {
            console.warn("音声出力デバイス一覧の取得に失敗:", err);
        }
    }

    _renderEngineStatus(st) {
        if (!st || Object.keys(st).length === 0) return;
        const pill = document.getElementById("engine-status-pill");
        if (pill) {
            const llmIcon = st.llm_ok ? "🟢" : "🔴";
            const ttsIcon = st.tts_ok ? "🟢" : "🟡";
            const ttsName = st.tts_provider === "aivisspeech" ? "Aivis" : st.tts_provider === "voicevox" ? "VV" : "TTS";
            const llmName = st.llm_provider === "ollama" ? "Ollama" : (st.llm_provider || "LLM");
            pill.textContent = `${llmIcon}${llmName} / ${ttsIcon}${ttsName}`;
            pill.classList.toggle("warn", !st.llm_ok || !st.tts_ok);
            pill.title = `最終確認: ${st.checked_at || "--"} | Ollama: ${st.ollama ? "起動" : "停止"} / AivisSpeech: ${st.aivisspeech ? "起動" : "停止"} / VOICEVOX: ${st.voicevox ? "起動" : "停止"}（クリックで再チェック）`;
        }
        const cacheEl = document.getElementById("audio-cache-stats");
        if (cacheEl && st.audio_cache) {
            cacheEl.textContent = `現在のキャッシュ: ${st.audio_cache.files}件 (${st.audio_cache.size_mb} MB)`;
        }
        const aivisBtn = document.getElementById("btn-start-aivis");
        if (aivisBtn) {
            const isUp = !!st.aivisspeech;
            aivisBtn.classList.toggle("running", isUp);
            aivisBtn.textContent = isUp ? "🟢 Aivis稼働中" : "🚀 Aivis裏起動";
            aivisBtn.title = isUp
                ? "AivisSpeechエンジン稼働中（クリックで二重起動せず状態確認＆表のGUIがあれば裏へ格納）"
                : "AivisSpeech をウィンドウを出さずに裏起動します（二重起動防止付き）";
        }
    }

    _renderDiscordStatus(st) {
        const lbl = document.getElementById("discord-status-label");
        if (!lbl || !st) return;
        const warn = st.message_content_warning ? " ⚠️ MessageContentIntent無効" : "";
        if (st.state === "connected") {
            const noAllow = !st.allowed_user_count ? "（⚠️ 許可User ID未設定のため応答停止中）" : "";
            lbl.textContent = `🟢 接続中 (${st.bot_user || "Bot"})${warn}${noAllow}`;
        } else if (st.state === "connecting") {
            lbl.textContent = "🟡 Discord Gateway へ接続中...";
        } else if (st.state === "error") {
            lbl.textContent = `🔴 エラー: ${st.error || "接続失敗"}`;
        } else {
            lbl.textContent = st.has_token ? "⚪ 停止中（トークン登録済み）" : "⚪ 停止中（トークン未設定）";
        }
    }

    _renderTopicWidget() {
        const widget = document.getElementById("obs-topic-widget");
        const topicEl = document.getElementById("obs-topic-text");
        const pomoEl = document.getElementById("obs-pomo-pill");
        if (!widget || !topicEl || !pomoEl) return;

        const cfg = this.currentConfig || {};
        const isVtuber = (cfg.mode || "agent") === "vtuber";
        const enabled = cfg.obs_topic_telop_enabled !== false;
        // AIエージェントモードではVTuber配信用の話題デッキ／お題ガチャを表示しない
        const topic = isVtuber ? (cfg.stream_topic || "").trim() : "";
        const pomo = cfg.pomodoro;
        const showPomo = !!(cfg.obs_telop_show_pomodoro !== false && pomo && pomo.active);

        if (!enabled || (!topic && !showPomo)) {
            widget.classList.add("hidden");
            return;
        }
        widget.classList.remove("hidden");
        if (topic) {
            topicEl.textContent = `📌 ${topic}`;
            topicEl.classList.remove("hidden");
        } else if (isVtuber) {
            topicEl.textContent = "📌 配信中";
            topicEl.classList.remove("hidden");
        } else {
            topicEl.textContent = "";
            topicEl.classList.add("hidden");
        }

        if (showPomo) {
            const sec = Math.max(0, Math.floor(pomo.remaining_sec || 0));
            const mm = String(Math.floor(sec / 60)).padStart(2, "0");
            const ss = String(sec % 60).padStart(2, "0");
            pomoEl.textContent = `${pomo.phase === "work" ? "🍅" : "☕"} ${mm}:${ss}`;
            pomoEl.classList.remove("hidden");
        } else {
            pomoEl.classList.add("hidden");
        }
    }

    _renderViewers(viewers) {
        const list = document.getElementById("viewer-list");
        if (!list) return;
        list.innerHTML = "";
        if (!viewers || !viewers.length) {
            list.innerHTML = `<li class="empty-item">記録されたリスナーはまだいません</li>`;
            return;
        }
        for (const v of viewers) {
            const li = document.createElement("li");
            li.className = "viewer-item";

            const info = document.createElement("div");
            info.className = "viewer-item-info";

            const nameRow = document.createElement("div");
            const badge = v.visit_count > 1 ? `常連(${v.visit_count}回)` : "初見";
            nameRow.innerHTML = `<span class="viewer-item-name"></span> <span class="viewer-item-meta"></span>`;
            nameRow.querySelector(".viewer-item-name").textContent = v.user_name;
            nameRow.querySelector(".viewer-item-meta").textContent = `${badge} · 発言${v.comment_count}${v.superchat_count ? ` · 💎${v.superchat_count}` : ""} · ${v.last_seen_str || ""}`;
            info.appendChild(nameRow);

            if (v.note) {
                const noteDiv = document.createElement("div");
                noteDiv.className = "viewer-item-note";
                noteDiv.textContent = v.note;
                info.appendChild(noteDiv);
            }

            const delBtn = document.createElement("button");
            delBtn.className = "del-btn";
            delBtn.textContent = "×";
            delBtn.title = "このリスナー記憶を削除";
            delBtn.addEventListener("click", () => {
                fetch(`/api/viewers/${encodeURIComponent(v.user_name)}`, { method: "DELETE" });
            });

            li.appendChild(info);
            li.appendChild(delBtn);
            list.appendChild(li);
        }
    }

    _setConnectionStatus(online) {
        const el = document.getElementById("ws-status");
        if (!el) return;
        el.textContent = online ? "ONLINE" : "RECONNECTING...";
        el.className = `status-pill ${online ? "online" : "offline"}`;
    }

    _setAgentStatus(text, busy) {
        const el = document.getElementById("agent-state-text");
        if (!el) return;
        el.textContent = text;
        el.className = busy ? "state-busy" : "state-idle";
    }

    _updateEmotionBadge(emotion) {
        const badge = document.getElementById("emotion-badge");
        if (badge) {
            badge.textContent = EMOTION_LABELS[emotion] || emotion.toUpperCase();
            badge.dataset.emotion = emotion;
        }
        document.querySelectorAll(".emotion-btn").forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.emotion === emotion);
        });
    }

    /**
     * 字幕自動消去タイマーをキャンセルする。
     */
    _clearSubtitleTimer() {
        if (this.subtitleHideTimer) {
            clearTimeout(this.subtitleHideTimer);
            this.subtitleHideTimer = null;
        }
        if (this.subtitleClearTextTimer) {
            clearTimeout(this.subtitleClearTextTimer);
            this.subtitleClearTextTimer = null;
        }
    }

    /**
     * ニアの下に表示される吹き出しをフェードアウトさせ、テキストとスペクトラムバーをクリアする。
     */
    _hideSubtitle() {
        this._clearSubtitleTimer();
        const box = document.getElementById("subtitle-box");
        const textEl = document.getElementById("subtitle-text");
        const subEl = document.getElementById("subtitle-sub-text");
        const viz = document.getElementById("audio-visualizer") || document.querySelector(".audio-visualizer");
        if (box) {
            box.classList.add("idle-hide");
        }
        if (viz) {
            viz.classList.add("idle-hide");
        }
        if (textEl) {
            this.subtitleClearTextTimer = setTimeout(() => {
                this.subtitleClearTextTimer = null;
                if (box && box.classList.contains("idle-hide")) {
                    textEl.textContent = "";
                    if (subEl) {
                        subEl.textContent = "";
                        subEl.classList.add("hidden");
                    }
                }
            }, 380);
        }
    }

    /**
     * 指定ミリ秒後に吹き出しを自動クリアするよう予約する。
     * @param {number} delayMs
     */
    _scheduleSubtitleClear(delayMs = 5000) {
        if (this.subtitleHideTimer) {
            clearTimeout(this.subtitleHideTimer);
            this.subtitleHideTimer = null;
        }
        this.subtitleHideTimer = setTimeout(() => {
            this.subtitleHideTimer = null;
            const stillAudio = !!(this.currentAudio && !this.currentAudio.paused && !this.currentAudio.ended);
            const stillStreaming = !!(this.speechStreamQueue && (this.speechStreamQueue.playing || !this.speechStreamQueue.finalized));
            if ((this.avatar && this.avatar.isSpeaking) || stillAudio || stillStreaming) {
                this._scheduleSubtitleClear(1800);
                return;
            }
            this._hideSubtitle();
        }, delayMs);
    }

    _showSubtitle(speech, emotion, autoClearDelayMs = null, speechSub = "") {
        this._clearSubtitleTimer();
        const box = document.getElementById("subtitle-box");
        const textEl = document.getElementById("subtitle-text");
        const subEl = document.getElementById("subtitle-sub-text");
        const viz = document.getElementById("audio-visualizer") || document.querySelector(".audio-visualizer");
        if (!box || !textEl) return;
        if (!speech || !String(speech).trim()) {
            this._hideSubtitle();
            return;
        }
        const alreadyVisibleSame =
            textEl.textContent === speech &&
            !box.classList.contains("idle-hide") &&
            !box.classList.contains("mascot-idle-hide");

        box.dataset.emotion = emotion || "normal";
        textEl.textContent = speech;
        if (subEl) {
            const cleanedSub = String(speechSub || "").trim();
            if (cleanedSub) {
                subEl.textContent = cleanedSub;
                subEl.classList.remove("hidden");
            } else {
                subEl.textContent = "";
                subEl.classList.add("hidden");
            }
        }
        box.classList.remove("idle-hide", "mascot-idle-hide");
        if (viz) {
            viz.classList.remove("idle-hide");
        }
        if (!alreadyVisibleSame) {
            box.classList.remove("pulse");
            void box.offsetWidth;
            box.classList.add("pulse");
        }

        // 音声が鳴らない場合や初期表示時のフォールバックとして一定時間後に吹き出しをクリアする
        const fallbackDelay = autoClearDelayMs ?? Math.max(7000, Math.min(15000, String(speech).length * 140));
        this._scheduleSubtitleClear(fallbackDelay);
    }

    _updateVisualizerBars(dataArray) {
        const bars = document.querySelectorAll(".viz-bar");
        if (!bars.length) return;
        bars.forEach((bar, idx) => {
            if (!dataArray) {
                bar.style.height = "4px";
            } else {
                const val = dataArray[(idx * 3) % dataArray.length] || 0;
                const h = Math.max(4, Math.min(36, (val / 255) * 38));
                bar.style.height = `${h}px`;
            }
        });
    }

    _renderSystemMetrics(m) {
        if (!m) return;
        const memVal = document.getElementById("metric-mem-val");
        const memBar = document.getElementById("metric-mem-bar");
        const diskVal = document.getElementById("metric-disk-val");
        const diskBar = document.getElementById("metric-disk-bar");
        const clockEl = document.getElementById("metric-clock");
        const winEl = document.getElementById("metric-active-win");

        if (memVal) memVal.textContent = `${m.memory_percent}% (${m.memory_used_gb}/${m.memory_total_gb}G)`;
        if (memBar) memBar.style.width = `${m.memory_percent}%`;
        if (diskVal) diskVal.textContent = `${m.disk_percent}% (空${m.disk_free_gb}G)`;
        if (diskBar) diskBar.style.width = `${m.disk_percent}%`;
        if (clockEl) clockEl.textContent = m.timestamp;
        if (winEl && m.active_window) {
            winEl.textContent = m.active_window + (m.is_fullscreen ? " [全画面]" : "");
        }
    }

    _appendThoughtItem(text, kind = "thought") {
        const container = document.getElementById("thought-log");
        if (!container) return;
        const item = document.createElement("div");
        item.className = `thought-item ${kind}`;
        const timeStr = new Date().toLocaleTimeString("ja-JP", { hour12: false });
        item.innerHTML = `<span class="time">[${timeStr}]</span> <span class="msg"></span>`;
        item.querySelector(".msg").textContent = text;
        container.appendChild(item);
        container.scrollTop = container.scrollHeight;
    }

    _appendFeedItem(entry) {
        const container = document.getElementById("chat-feed");
        if (!container) return;
        const div = document.createElement("div");
        const isSuperchat = entry.source === "superchat";
        div.className = `feed-item ${entry.role} ${entry.source || ""} ${isSuperchat ? "superchat" : ""}`;

        const header = document.createElement("div");
        header.className = "feed-header";
        let badge = "";
        if (isSuperchat) {
            badge = `<span class="sc-badge">SUPERCHAT ${(entry.metadata && entry.metadata.amount) || "¥1,000"}</span>`;
        } else if (entry.source === "discord") {
            badge = `<span class="src-badge discord">DISCORD</span>`;
        } else if (entry.source === "live_comment") {
            badge = `<span class="src-badge">VIEWER</span>`;
        } else if (entry.source === "proactive" || entry.source === "silence_breaker") {
            badge = `<span class="src-badge auto">AUTO</span>`;
        } else if (entry.emotion) {
            badge = `<span class="emo-tag">${entry.emotion.toUpperCase()}</span>`;
        }
        header.innerHTML = `<strong class="user-name"></strong> ${badge}`;
        header.querySelector(".user-name").textContent = entry.user;

        div.appendChild(header);

        if (Array.isArray(entry.evidences) && entry.evidences.length > 0) {
            const combinedEvidence = entry.evidences
                .map((e) => (e.data || "").trim())
                .filter(Boolean)
                .join("\n\n");
            if (combinedEvidence) {
                const lines = combinedEvidence.split(/\r?\n/).filter((ln) => ln.trim().length > 0);
                const evBox = document.createElement("details");
                evBox.className = "feed-evidence-box";
                // 簡潔な引用一覧（7行以下）は初期展開し、長文ダンプは自動折りたたみにして返答文の視認性を保つ
                evBox.open = lines.length <= 7;

                const evHead = document.createElement("summary");
                evHead.className = "feed-evidence-header";
                const tools = [...new Set(entry.evidences.map((e) => e.tool).filter(Boolean))].join(", ");
                const metaSuffix = lines.length > 7 ? ` ・ ${lines.length}行（クリックで展開）` : "";
                evHead.textContent = `📊 参照データ・引用元${tools ? ` (${tools})` : ""}${metaSuffix}`;

                const evBody = document.createElement("div");
                evBody.className = "feed-evidence-body";
                this._renderEvidenceLines(evBody, combinedEvidence);

                evBox.appendChild(evHead);
                evBox.appendChild(evBody);
                div.appendChild(evBox);
            }
        }

        const body = document.createElement("div");
        body.className = "feed-body";
        body.textContent = entry.content;

        div.appendChild(body);
        if (entry.speechSub && String(entry.speechSub).trim()) {
            const subDiv = document.createElement("div");
            subDiv.className = "feed-sub-speech";
            subDiv.textContent = entry.speechSub;
            div.appendChild(subDiv);
        }
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    _renderEvidenceLines(container, rawText) {
        // 引用データ内の [ラベル](URL) および生URLを安全なクリック可能リンク（短縮ドメイン表示）へ変換する
        const linkRegex = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>)\]]+)/g;
        const lines = rawText.split(/\r?\n/);

        for (const line of lines) {
            if (!line.trim()) {
                const spacer = document.createElement("div");
                spacer.className = "feed-evidence-spacer";
                container.appendChild(spacer);
                continue;
            }

            const lineEl = document.createElement("div");
            lineEl.className = "feed-evidence-line";
            let lastIdx = 0;
            linkRegex.lastIndex = 0;
            let match;

            while ((match = linkRegex.exec(line)) !== null) {
                if (match.index > lastIdx) {
                    lineEl.appendChild(document.createTextNode(line.slice(lastIdx, match.index)));
                }

                const mdLabel = match[1];
                const url = match[2] || match[3] || match[4];
                if (url && /^https?:\/\//i.test(url)) {
                    const anchor = document.createElement("a");
                    anchor.className = "feed-evidence-link";
                    anchor.href = url;
                    anchor.target = "_blank";
                    anchor.rel = "noopener noreferrer";
                    anchor.title = url;

                    if (mdLabel) {
                        anchor.textContent = mdLabel;
                    } else {
                        let hostLabel = "リンク";
                        try {
                            hostLabel = new URL(url).hostname.replace(/^www\./, "");
                        } catch (_) {
                            hostLabel = "リンク";
                        }
                        anchor.textContent = `${hostLabel} ↗`;
                    }
                    lineEl.appendChild(anchor);
                } else {
                    lineEl.appendChild(document.createTextNode(match[0]));
                }

                lastIdx = linkRegex.lastIndex;
            }

            if (lastIdx < line.length) {
                lineEl.appendChild(document.createTextNode(line.slice(lastIdx)));
            }
            container.appendChild(lineEl);
        }
    }

    _renderInitialHistory(history, options = {}) {
        const { showSubtitle = true, appendThoughts = true } = options;
        const container = document.getElementById("chat-feed");
        if (!container) return;
        container.innerHTML = "";
        if (!history || history.length === 0) {
            if (showSubtitle) {
                const def = this._getPersonaDefaultSpeech();
                this._showSubtitle(def.speech, def.emotion);
            }
            return;
        }
        for (const h of history) {
            this._appendFeedItem({
                role: "user",
                source: h.source,
                user: h.user_name,
                content: h.user_input,
            });
            this._appendFeedItem({
                role: "agent",
                user: this.currentConfig.agent_name || "ニア (NIA)",
                emotion: h.emotion,
                content: h.agent_speech,
                speechSub: h.speech_sub || "",
                evidences: h.evidences || [],
            });
            if (appendThoughts && h.thought) {
                this._appendThoughtItem(`[${h.emotion}] ${h.thought}`, "thought");
            }
        }
        if (showSubtitle) {
            const last = history[history.length - 1];
            if (last && last.agent_speech) {
                this.avatar.setEmotion("normal");
                this._updateEmotionBadge("normal");
                this._showSubtitle(last.agent_speech, last.emotion || "normal", null, last.speech_sub || "");
            }
        }
    }

    _renderMemories(memories) {
        const list = document.getElementById("memory-list");
        if (!list) return;
        list.innerHTML = "";
        if (!memories || !memories.length) {
            list.innerHTML = `<li class="empty-item">記録された長期記憶はありません</li>`;
            return;
        }
        const categoryLabels = {
            persona_agent: "🛡️ エージェント設定",
            persona_vtuber: "📺 VTuber設定",
            user_pref: "👤 ユーザー好み",
            fact: "📌 事実・知識",
            research: "🔍 調査メモ",
            memo: "📝 メモ",
        };
        for (const m of memories) {
            const li = document.createElement("li");
            const isPersonaSync = m.category === "persona_agent" || m.category === "persona_vtuber";
            li.className = `memory-item ${isPersonaSync ? "persona-sync" : ""}`;

            const badgeLabel = categoryLabels[m.category] || `📌 ${m.category}`;
            const cleanedText = String(m.content || "")
                .replace(/^\[(AIエージェント設定|VTuber設定)\]\s*/, "")
                .trim();

            const head = document.createElement("div");
            head.className = "memory-item-head";

            const catSpan = document.createElement("span");
            catSpan.className = "cat";
            catSpan.textContent = badgeLabel;
            const semScore = m.similarity_score ?? m.semantic_score;
            if (typeof semScore === "number") {
                const scoreBadge = document.createElement("span");
                scoreBadge.className = "semantic-badge";
                scoreBadge.textContent = `類似 ${Math.round(semScore * 100)}%`;
                catSpan.appendChild(scoreBadge);
            }
            head.appendChild(catSpan);

            const rightGroup = document.createElement("div");
            rightGroup.className = "mem-head-right";

            if (isPersonaSync) {
                const syncBadge = document.createElement("span");
                syncBadge.className = "sync-tag";
                syncBadge.textContent = "設定連動";
                rightGroup.appendChild(syncBadge);
            } else {
                const starBtn = document.createElement("button");
                starBtn.className = "star-btn";
                starBtn.title = "クリックして重要度(★1〜5)を変更";
                const imp = Number(m.importance) || 3;
                starBtn.textContent = `★${imp}`;
                starBtn.addEventListener("click", () => {
                    const nextImp = imp >= 5 ? 1 : imp + 1;
                    fetch(`/api/memories/${m.id}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ importance: nextImp }),
                    });
                });
                rightGroup.appendChild(starBtn);

                const delBtn = document.createElement("button");
                delBtn.className = "del-btn";
                delBtn.title = "この記憶を削除";
                delBtn.textContent = "×";
                delBtn.addEventListener("click", () => {
                    fetch(`/api/memories/${m.id}`, { method: "DELETE" });
                });
                rightGroup.appendChild(delBtn);
            }
            head.appendChild(rightGroup);

            const body = document.createElement("div");
            body.className = "memory-item-body";
            body.textContent = cleanedText;
            body.title = cleanedText;

            li.appendChild(head);
            li.appendChild(body);
            list.appendChild(li);
        }
    }

    _renderTasks(tasks) {
        const list = document.getElementById("task-list");
        if (!list) return;
        list.innerHTML = "";
        if (!tasks || !tasks.length) {
            list.innerHTML = `<li class="empty-item">未完了の予定・タスクはありません</li>`;
            return;
        }
        for (const t of tasks) {
            const li = document.createElement("li");
            li.className = "task-item";

            const top = document.createElement("div");
            top.className = "task-item-top";

            const titleSpan = document.createElement("span");
            titleSpan.className = "txt";
            const repIcon = t.repeat_rule && t.repeat_rule !== "none" ? `🔁` : "";
            titleSpan.textContent = `${repIcon}${t.title}`;
            top.appendChild(titleSpan);

            const actions = document.createElement("div");
            actions.className = "task-actions";

            const snoozeBtn = document.createElement("button");
            snoozeBtn.className = "snooze-btn";
            snoozeBtn.title = `${this.currentConfig.schedule_snooze_min || 5}分スヌーズ`;
            snoozeBtn.textContent = "🔔";
            snoozeBtn.addEventListener("click", () => {
                fetch(`/api/tasks/${t.id}/snooze`, { method: "POST" });
            });
            actions.appendChild(snoozeBtn);

            const doneBtn = document.createElement("button");
            doneBtn.className = "done-btn";
            doneBtn.textContent = "完了";
            doneBtn.addEventListener("click", () => {
                fetch(`/api/tasks/${t.id}/complete`, { method: "POST" });
            });
            actions.appendChild(doneBtn);

            const delBtn = document.createElement("button");
            delBtn.className = "del-btn";
            delBtn.textContent = "×";
            delBtn.addEventListener("click", () => {
                fetch(`/api/tasks/${t.id}`, { method: "DELETE" });
            });
            actions.appendChild(delBtn);

            top.appendChild(actions);
            li.appendChild(top);

            if (t.due_str) {
                const sub = document.createElement("div");
                sub.className = "task-due-label";
                sub.textContent = `⏰ 通知予定: ${t.due_str}`;
                li.appendChild(sub);
            }

            list.appendChild(li);
        }
    }

    _renderRagStats(stats) {
        const badge = document.getElementById("rag-stats-badge");
        if (!badge || !stats) return;
        badge.textContent = `RAG: ${stats.total_docs || 0}文書`;
        badge.title = `インデックスチャンク数: ${stats.total_chunks || 0} / フォルダ: ${stats.rag_docs_dir || ""}`;
    }

    _renderStreamSessionBadge(session) {
        const btn = document.getElementById("btn-stream-session-toggle");
        if (!btn) return;
        if (session && session.status === "live") {
            btn.classList.add("active");
            btn.textContent = "⏹️ 配信終了＆チャプター生成";
        } else {
            btn.classList.remove("active");
            btn.textContent = "🔴 配信記録開始";
        }
    }

    _renderWorkflows(workflows) {
        const list = document.getElementById("workflow-list");
        if (!list) return;
        list.innerHTML = "";
        if (!workflows || !workflows.length) {
            list.innerHTML = `<li class="empty-item">定時ワークフロー未登録（＋追加で登録可）</li>`;
            return;
        }
        for (const wf of workflows) {
            const li = document.createElement("li");
            li.className = "task-item";
            const top = document.createElement("div");
            top.className = "task-item-top";

            const titleSpan = document.createElement("span");
            titleSpan.className = "txt";
            titleSpan.textContent = `${wf.enabled ? "🟢" : "⚪"} ${wf.name}`;
            top.appendChild(titleSpan);

            const actions = document.createElement("div");
            actions.className = "task-actions";

            const runBtn = document.createElement("button");
            runBtn.className = "done-btn";
            runBtn.textContent = "▶";
            runBtn.title = "今すぐ実行";
            runBtn.addEventListener("click", () => fetch(`/api/agent/workflows/${wf.id}/run`, { method: "POST" }));
            actions.appendChild(runBtn);

            const togBtn = document.createElement("button");
            togBtn.className = "snooze-btn";
            togBtn.textContent = wf.enabled ? "⏸" : "▶";
            togBtn.title = "有効/無効切替";
            togBtn.addEventListener("click", () => fetch(`/api/agent/workflows/${wf.id}/toggle`, { method: "POST" }));
            actions.appendChild(togBtn);

            const delBtn = document.createElement("button");
            delBtn.className = "del-btn";
            delBtn.textContent = "×";
            delBtn.addEventListener("click", () => fetch(`/api/agent/workflows/${wf.id}`, { method: "DELETE" }));
            actions.appendChild(delBtn);

            top.appendChild(actions);
            li.appendChild(top);

            const sub = document.createElement("div");
            sub.className = "task-due-label";
            sub.textContent = `${wf.workflow_type} · 次回: ${wf.next_run_str} (最終: ${wf.last_run_str})`;
            li.appendChild(sub);
            list.appendChild(li);
        }
    }

    _renderResearchJobs(jobs) {
        const list = document.getElementById("research-list");
        if (!list) return;
        list.innerHTML = "";
        if (!jobs || !jobs.length) {
            list.innerHTML = `<li class="empty-item">実行中/完了した調査はありません</li>`;
            return;
        }
        for (const job of jobs) {
            const li = document.createElement("li");
            li.className = "task-item";
            const icon = job.status === "completed" ? "✅" : (job.status === "error" ? "❌" : "⏳");
            li.textContent = `${icon} ${job.topic} (${job.created_at})`;
            if (job.report_path) li.title = job.report_path;
            list.appendChild(li);
        }
    }

    _renderPendingApprovals() {
        const container = document.getElementById("approval-banner-container");
        if (!container) return;
        container.innerHTML = "";
        if (!this.pendingApprovals || this.pendingApprovals.length === 0) {
            container.classList.add("hidden");
            return;
        }
        container.classList.remove("hidden");
        for (const req of this.pendingApprovals) {
            const card = document.createElement("div");
            card.className = "approval-card";

            const title = document.createElement("div");
            title.textContent = `🛡️ コマンド実行承認リクエスト (${req.id}) — ${req.reason || ""}`;
            const code = document.createElement("code");
            code.textContent = req.command;

            const actions = document.createElement("div");
            actions.className = "approval-actions";

            const allowBtn = document.createElement("button");
            allowBtn.className = "approval-btn allow";
            allowBtn.textContent = "✅ 許可して実行";
            allowBtn.addEventListener("click", () => {
                fetch(`/api/agent/approvals/${req.id}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ approved: true }),
                });
            });

            const denyBtn = document.createElement("button");
            denyBtn.className = "approval-btn deny";
            denyBtn.textContent = "❌ 拒否";
            denyBtn.addEventListener("click", () => {
                fetch(`/api/agent/approvals/${req.id}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ approved: false }),
                });
            });

            actions.appendChild(allowBtn);
            actions.appendChild(denyBtn);
            card.appendChild(title);
            card.appendChild(code);
            card.appendChild(actions);
            container.appendChild(card);
        }
    }

    _renderNewsOverlay(cards) {
        const box = document.getElementById("obs-news-card-box");
        if (!box) return;
        if (this.newsOverlayTimer) clearTimeout(this.newsOverlayTimer);
        if (!cards || !cards.length) {
            box.classList.add("hidden");
            return;
        }
        box.innerHTML = "";
        const head = document.createElement("div");
        head.className = "obs-news-title";
        head.textContent = "📰 LIVE TOPIC // 引用ニュース";
        box.appendChild(head);

        for (const c of cards) {
            const item = document.createElement("div");
            item.className = "obs-news-item";
            const tag = document.createElement("span");
            tag.className = "obs-news-tag";
            tag.textContent = c.tag || "NEWS";
            const txt = document.createElement("span");
            txt.textContent = c.title || "";
            item.appendChild(tag);
            item.appendChild(txt);
            box.appendChild(item);
        }
        box.classList.remove("hidden");
        this.newsOverlayTimer = setTimeout(() => box.classList.add("hidden"), 18000);
    }

    _renderPollOverlay(poll) {
        const box = document.getElementById("obs-poll-overlay");
        if (!box) return;
        if (!poll || !poll.active) {
            box.classList.add("hidden");
            return;
        }
        box.innerHTML = "";
        const title = document.createElement("div");
        title.className = "obs-news-title";
        title.textContent = `📊 ${poll.title} (${poll.remaining_sec}s / 計${poll.total_votes}票)`;
        box.appendChild(title);

        if (poll.game_type === "poll" && Array.isArray(poll.options)) {
            for (const opt of poll.options) {
                const wrap = document.createElement("div");
                wrap.className = "obs-poll-bar-wrap";
                const lbl = document.createElement("div");
                lbl.className = "obs-poll-label";
                lbl.innerHTML = `<span></span><span></span>`;
                lbl.firstChild.textContent = `${opt.index}. ${opt.label}`;
                lbl.lastChild.textContent = `${opt.count}票 (${opt.percent}%)`;

                const track = document.createElement("div");
                track.className = "obs-poll-track";
                const fill = document.createElement("div");
                fill.className = "obs-poll-fill";
                fill.style.width = `${opt.percent}%`;
                track.appendChild(fill);
                wrap.appendChild(lbl);
                wrap.appendChild(track);
                box.appendChild(wrap);
            }
        } else {
            const subInfo = document.createElement("div");
            subInfo.textContent = `回答募集中！現在 ${poll.submission_count || 0} 件の回答が届いています`;
            box.appendChild(subInfo);
        }
        box.classList.remove("hidden");
    }

    _renderMailbagOverlay(item) {
        const box = document.getElementById("obs-mailbag-card");
        if (!box) return;
        if (this.mailbagOverlayTimer) clearTimeout(this.mailbagOverlayTimer);
        if (!item) {
            box.classList.add("hidden");
            return;
        }
        box.innerHTML = "";
        const head = document.createElement("div");
        head.className = "obs-news-title";
        head.textContent = `📮 お便り: ${item.sender || "匿名"}`;
        const body = document.createElement("div");
        body.textContent = item.question || "";
        box.appendChild(head);
        box.appendChild(body);
        box.classList.remove("hidden");
        this.mailbagOverlayTimer = setTimeout(() => box.classList.add("hidden"), 22000);
    }

    _renderKaraokeTracks(tracks) {
        const sel = document.getElementById("select-karaoke-track");
        if (!sel) return;
        sel.innerHTML = "";
        for (const t of tracks || []) {
            const opt = document.createElement("option");
            opt.value = t.id;
            opt.textContent = t.title;
            sel.appendChild(opt);
        }
    }

    _playKaraokeTrack(track) {
        const bar = document.getElementById("obs-karaoke-bar");
        if (this.karaokeAudio) {
            try { this.karaokeAudio.pause(); } catch (_) {}
            this.karaokeAudio = null;
        }
        if (this.karaokeTimer) {
            clearInterval(this.karaokeTimer);
            this.karaokeTimer = null;
        }
        if (!track || !track.audio_url) return;

        const audio = new Audio(track.audio_url);
        this.karaokeAudio = audio;
        const lyrics = track.lyrics || [];
        if (bar) {
            bar.innerHTML = "";
            const tHead = document.createElement("div");
            tHead.className = "obs-news-title";
            tHead.textContent = `🎤 NOW SINGING: ${track.title}`;
            const lBody = document.createElement("div");
            lBody.className = "obs-karaoke-lyric";
            lBody.textContent = lyrics.length > 0 ? lyrics[0].text : "♪ 演奏中... ♪";
            bar.appendChild(tHead);
            bar.appendChild(lBody);
            bar.classList.remove("hidden");

            this.karaokeTimer = setInterval(() => {
                if (!this.karaokeAudio || this.karaokeAudio.ended) {
                    clearInterval(this.karaokeTimer);
                    this.karaokeTimer = null;
                    setTimeout(() => bar.classList.add("hidden"), 1500);
                    return;
                }
                const cur = this.karaokeAudio.currentTime || 0;
                for (let i = lyrics.length - 1; i >= 0; i--) {
                    if (cur >= lyrics[i].time_sec) {
                        lBody.textContent = lyrics[i].text;
                        break;
                    }
                }
            }, 120);
        }
        audio.onended = () => {
            if (bar) setTimeout(() => bar.classList.add("hidden"), 1500);
            this.sendChatMessage(
                `【歌枠MC】曲「${track.title}」を歌い終わりました！感想とリスナーへの合いの手・盛り上げコメントを1〜2文で喋ってください。`,
                "silence_breaker",
                "KaraokeHost"
            );
        };
        audio.play().catch((err) => console.warn("歌枠再生エラー:", err));
    }
}

window.addEventListener("DOMContentLoaded", () => {
    window.agentApp = new AgentController();
});
