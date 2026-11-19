/**
 * キャラクターアバター（NIA / ニア）のCanvas 2D高精細リアルタイム描画エンジン。
 *
 * - 6種類の感情（normal, smile, joy, serious, surprised, troubled）
 * - 目を開けた状態の6口形（open_normal, open_a, open_i, open_u, open_e, open_o）※開口量は自然な半開口に調整済み
 * - 目を閉じた状態の6口形（blink_normal, blink_a, blink_i, blink_u, blink_e, blink_o）
 * - 全身立ち絵 6種類（full_normal 〜 full_troubled）※全身表示時も顔領域オーバーレイにより瞬き＆口パク対応
 */

const EMOTIONS = ["normal", "smile", "joy", "serious", "surprised", "troubled"];
const EYE_STATES = ["open", "blink"];
const VOWELS = ["normal", "a", "i", "u", "e", "o"];

const KANA_LIPSYNC_MAP = {
    a: "あかさたなはまやらわがざだばぱぁゃアカサタナハマヤラワガザダバパァャ",
    e: "えけせてねへめれげぜでべぺぇエケセテネヘメレゲゼデベペェいきしちにひみりぎじぢびぴぃイキシチニヒミリギジヂビピィ",
    u: "うくすつぬふむゆるぐずづぶぷぅゅヴウクスツヌフムユルグズヅブプゥュ",
    o: "おこそとのほもよろをごぞどぼぽぉょオコソトノホモヨロヲゴゾドボポォョ",
    normal: "んっンッ、。！？…",
};

export class AvatarRenderer {
    /**
     * @param {HTMLCanvasElement} canvas 描画対象のCanvas要素
     * @param {boolean} isObsMode OBS透過モードかどうか
     */
    constructor(canvas, isObsMode = false) {
        this.canvas = canvas;
        this.ctx = canvas.getContext("2d");
        this.isObsMode = isObsMode;
        this.isMascotMode = document.body.classList.contains("mascot-mode");
        if (this.isMascotMode) {
            // 縦長マスコットウィンドウ全体を上端〜下端までフルに使える縦長高解像度キャンバスに設定
            this.canvas.width = 680;
            this.canvas.height = 1000;
        }

        // 現在の状態
        this.currentEmotion = "normal";
        this.viewMode = "bust"; // "bust" (バストアップ) または "full" (全身立ち絵)
        this.audioLevel = 0.0;  // 0.0 〜 1.0 のリアルタイム音量レベル
        this.smoothAudio = 0.0; // エンベロープ平滑化後の音量レベル
        this.formantHint = null; // 周波数バランスに基づく推定母音ヒント ("a" | "i" | "u" | "e" | "o" | null)
        this.isSpeaking = false;
        this.speechSource = "none"; // "none" | "webaudio" | "browser"
        this.speechExpectedEndTime = 0;
        this.isThinking = false;
        this.actionEffectType = "none";
        this.actionEffectUntil = 0;

        // 手動プレビュー固定モード（null の場合は自動リップシンク＆自動瞬き）
        this.manualVowel = null; // null | "normal" | "a" | "i" | "u" | "e" | "o"
        this.forceBlink = false; // true の場合は常時目閉じ状態を表示

        // 発話テキストに基づく母音タイムラインとチャタリング防止ホールド制御
        this.speechVowels = [];
        this.speechStartTime = 0;
        this.lastVowel = "normal";
        this.lastVowelChangeTime = 0;

        // 瞬きタイマー制御
        this.isBlinking = false;
        this.nextBlinkTime = performance.now() + 2800;
        this.blinkEndTime = 0;

        // マウス追従（微細な呼吸モーション）および感情モーション演出
        this.targetLookX = 0;
        this.targetLookY = 0;
        this.currentLookX = 0;
        this.currentLookY = 0;
        this.motionIntensity = 1.0;
        this.emotionTriggerTime = 0;

        // 着せ替え衣装・アクセサリー・環境演出・ふれあいインタラクション状態
        this.activeOutfit = "default";
        this.activeAccessories = [];
        this.outfitImages = {}; // { [id]: { full: Image, bust: Image } }
        this.outfitChangeUntil = 0;
        this.environment = { time_period: "day", weather_kind: "clear" };
        this.weatherBgEnabled = true;
        this.interactionEnabled = true;
        this.pointerMode = "interact"; // "interact" (なでなで/つつき) | "pan" (位置移動)
        this.onInteract = null; // (kind: "pet" | "poke") => void
        this.blushUntil = 0;
        this.hearts = [];
        this.raindrops = Array.from({ length: 28 }, () => ({
            x: Math.random(),
            y: Math.random(),
            speed: 0.008 + Math.random() * 0.009,
            len: 12 + Math.random() * 14,
        }));
        this.stars = Array.from({ length: 24 }, () => ({
            x: Math.random(),
            y: Math.random() * 0.55,
            phase: Math.random() * Math.PI * 2,
            size: 1.2 + Math.random() * 1.8,
        }));
        this._petAccumDist = 0;
        this._petTriggeredInDrag = false;
        this._downClientX = 0;
        this._downClientY = 0;
        this._lastInteractFire = 0;

        // モード別の表示倍率(zoom)と上下左右位置オフセット(panX, panY)
        // 全身モード("full")はデフォルトで1.45倍＋下方向シフト(panY: 115)にし、顔〜上半身が小さくならないよう最適化
        this.defaultTransforms = {
            bust: { zoom: 1.0, panX: 0, panY: 0 },
            full: { zoom: 1.45, panX: 0, panY: 115 },
        };
        this.cameraTransforms = this._loadCameraTransforms();
        this._isDragging = false;
        this._dragLastX = 0;
        this._dragLastY = 0;

        // 高解像度スプライトキャッシュ
        this.sprites = {};
        this.fullSprites = {};
        this.loadedCount = 0;
        this.totalAssets = EMOTIONS.length * EYE_STATES.length * VOWELS.length + EMOTIONS.length;

        this._preloadAssets();
        this._setupMouseTracking();
        this._setupZoomAndPanControls();
        this._startAnimationLoop();
    }

    _loadCameraTransforms() {
        try {
            const raw = localStorage.getItem("nia_avatar_camera_v1");
            if (raw) {
                const parsed = JSON.parse(raw);
                return {
                    bust: { ...this.defaultTransforms.bust, ...(parsed.bust || {}) },
                    full: { ...this.defaultTransforms.full, ...(parsed.full || {}) },
                };
            }
        } catch (_) {}
        return {
            bust: { ...this.defaultTransforms.bust },
            full: { ...this.defaultTransforms.full },
        };
    }

    _saveCameraTransforms() {
        try {
            localStorage.setItem("nia_avatar_camera_v1", JSON.stringify(this.cameraTransforms));
        } catch (_) {}
    }

    /**
     * 現在の表示モード(bust/full)に対する倍率・上下左右位置オブジェクトを返す。
     */
    _getCurrentTransform() {
        return this.cameraTransforms[this.viewMode] || this.cameraTransforms.bust;
    }

    /**
     * 倍率を増減する（正で拡大、負で縮小）。
     * @param {number} deltaZoom
     */
    adjustZoom(deltaZoom) {
        const t = this._getCurrentTransform();
        t.zoom = Math.max(0.65, Math.min(3.0, Math.round((t.zoom + deltaZoom) * 100) / 100));
        this._saveCameraTransforms();
    }

    /**
     * 上下左右の位置オフセットを移動する（deltaY < 0 で上へ、deltaY > 0 で下へ移動）。
     * @param {number} deltaX
     * @param {number} deltaY
     */
    adjustPan(deltaX, deltaY) {
        const t = this._getCurrentTransform();
        t.panX = Math.max(-500, Math.min(500, Math.round(t.panX + deltaX)));
        t.panY = Math.max(-600, Math.min(600, Math.round(t.panY + deltaY)));
        this._saveCameraTransforms();
    }

    /**
     * 現在の表示モードの倍率・上下位置を初期おすすめ値へリセットする。
     */
    resetTransform() {
        const def = this.defaultTransforms[this.viewMode] || { zoom: 1.0, panX: 0, panY: 0 };
        this.cameraTransforms[this.viewMode] = { ...def };
        this._saveCameraTransforms();
    }

    /**
     * Canvas上のマウスホイール（拡大縮小）およびドラッグ（上下左右移動）をバインドする。
     */
    _setupZoomAndPanControls() {
        this.canvas.addEventListener(
            "wheel",
            (e) => {
                e.preventDefault();
                const delta = e.deltaY < 0 ? 0.08 : -0.08;
                this.adjustZoom(delta);
            },
            { passive: false }
        );

        this.canvas.addEventListener("mousedown", (e) => {
            if (e.button !== 0) return;
            this._isDragging = true;
            this._dragLastX = e.clientX;
            this._dragLastY = e.clientY;
            this._downClientX = e.clientX;
            this._downClientY = e.clientY;
            this._petAccumDist = 0;
            this._petTriggeredInDrag = false;
            const usePan = this.pointerMode === "pan" || e.shiftKey || !this.interactionEnabled;
            this._dragIsPan = usePan;
            this.canvas.style.cursor = usePan ? "grabbing" : "pointer";
        });

        window.addEventListener("mousemove", (e) => {
            if (!this._isDragging) return;
            const rect = this.canvas.getBoundingClientRect();
            const scaleX = rect.width > 0 ? this.canvas.width / rect.width : 1.0;
            const scaleY = rect.height > 0 ? this.canvas.height / rect.height : 1.0;
            const dx = (e.clientX - this._dragLastX) * scaleX;
            const dy = (e.clientY - this._dragLastY) * scaleY;
            this._dragLastX = e.clientX;
            this._dragLastY = e.clientY;

            if (this._dragIsPan) {
                this.adjustPan(dx, dy);
                return;
            }
            // ふれあいモード: キャラ上半分で左右になでると「頭なでなで」発動
            const relY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;
            if (relY <= 0.56) {
                this._petAccumDist += Math.abs(dx);
                if (this._petAccumDist >= 65 && !this._petTriggeredInDrag) {
                    this._petTriggeredInDrag = true;
                    this._triggerLocalInteraction("pet", (e.clientX - rect.left) * scaleX, (e.clientY - rect.top) * scaleY);
                }
            }
        });

        window.addEventListener("mouseup", (e) => {
            if (this._isDragging) {
                const moved = Math.hypot(e.clientX - this._downClientX, e.clientY - this._downClientY);
                const wasPan = this._dragIsPan;
                this._isDragging = false;
                this.canvas.style.cursor = this.pointerMode === "pan" ? "grab" : "pointer";
                if (!wasPan && !this._petTriggeredInDrag && moved < 10 && this.interactionEnabled) {
                    const rect = this.canvas.getBoundingClientRect();
                    if (
                        e.clientX >= rect.left &&
                        e.clientX <= rect.right &&
                        e.clientY >= rect.top &&
                        e.clientY <= rect.bottom
                    ) {
                        const scaleX = rect.width > 0 ? this.canvas.width / rect.width : 1.0;
                        const scaleY = rect.height > 0 ? this.canvas.height / rect.height : 1.0;
                        this._triggerLocalInteraction(
                            "poke",
                            (e.clientX - rect.left) * scaleX,
                            (e.clientY - rect.top) * scaleY
                        );
                    }
                }
            }
        });
        this.canvas.style.cursor = "pointer";
    }

    setPointerMode(mode) {
        this.pointerMode = mode === "pan" ? "pan" : "interact";
        this.canvas.style.cursor = this.pointerMode === "pan" ? "grab" : "pointer";
    }

    setOutfit(outfitId, withSparkle = true) {
        const nextId = (outfitId || "default").trim().toLowerCase();
        const changed = this.activeOutfit !== nextId;
        this.activeOutfit = nextId;
        if (nextId !== "default" && !this.outfitImages[nextId]) {
            const bust = new Image();
            bust.src = `/assets/outfits/${encodeURIComponent(nextId)}/bust.png?v=1`;
            const full = new Image();
            full.src = `/assets/outfits/${encodeURIComponent(nextId)}/full.png?v=1`;
            this.outfitImages[nextId] = { bust, full };
        }
        if (changed && withSparkle) {
            this.outfitChangeUntil = performance.now() + 1150;
            this.emotionTriggerTime = performance.now();
        }
    }

    setAccessories(accList) {
        this.activeAccessories = Array.isArray(accList) ? [...accList] : [];
    }

    setEnvironment(env, weatherBgEnabled = true) {
        if (env && typeof env === "object") {
            this.environment = { ...this.environment, ...env };
        }
        this.weatherBgEnabled = !!weatherBgEnabled;
    }

    _triggerLocalInteraction(kind, x, y) {
        const now = performance.now();
        if (now - this._lastInteractFire < 900) return;
        this._lastInteractFire = now;
        this.blushUntil = now + 4500;
        if (kind === "pet") {
            this.setEmotion("smile");
            for (let i = 0; i < 8; i++) {
                this.hearts.push({
                    x: x + (Math.random() - 0.5) * 90,
                    y: y + (Math.random() - 0.5) * 40,
                    vy: -1.1 - Math.random() * 1.1,
                    vx: (Math.random() - 0.5) * 0.8,
                    born: now,
                    life: 1600 + Math.random() * 600,
                    kind: i % 3 === 0 ? "sparkle" : "heart",
                });
            }
        } else {
            this.setEmotion("surprised");
            for (let i = 0; i < 5; i++) {
                this.hearts.push({
                    x: x + (Math.random() - 0.5) * 70,
                    y: y + (Math.random() - 0.5) * 30,
                    vy: -1.3 - Math.random() * 0.9,
                    vx: (Math.random() - 0.5) * 1.2,
                    born: now,
                    life: 1100 + Math.random() * 400,
                    kind: "sparkle",
                });
            }
        }
        if (typeof this.onInteract === "function") {
            this.onInteract(kind);
        }
    }

    /**
     * 6感情 × 目開け/目閉じ × 6母音（計72枚）および全身立ち絵6枚を事前読み込みする。
     */
    _preloadAssets() {
        const cacheBuster = `?v=4`;
        for (const emotion of EMOTIONS) {
            this.sprites[emotion] = { open: {}, blink: {} };
            for (const eye of EYE_STATES) {
                for (const vowel of VOWELS) {
                    const img = new Image();
                    img.src = `/assets/character/${emotion}_${eye}_${vowel}.png${cacheBuster}`;
                    img.onload = () => {
                        this.loadedCount++;
                    };
                    this.sprites[emotion][eye][vowel] = img;
                }
            }

            const fullImg = new Image();
            fullImg.src = `/assets/character/full_${emotion}.png${cacheBuster}`;
            fullImg.onload = () => {
                this.loadedCount++;
            };
            this.fullSprites[emotion] = fullImg;
        }
    }

    /**
     * マウス移動に応じた自然な揺れを設定する。
     */
    _setupMouseTracking() {
        window.addEventListener("mousemove", (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            this.targetLookX = Math.max(-1, Math.min(1, (e.clientX - cx) / (window.innerWidth * 0.45)));
            this.targetLookY = Math.max(-1, Math.min(1, (e.clientY - cy) / (window.innerHeight * 0.45)));
        });
    }

    /**
     * 感情を切り替え、感情別モーション演出をトリガーする。
     * @param {string} emotion 感情キー ("normal" | "smile" | "joy" | "serious" | "surprised" | "troubled")
     */
    setEmotion(emotion) {
        const normalized = (emotion || "normal").toLowerCase();
        if (EMOTIONS.includes(normalized)) {
            if (this.currentEmotion !== normalized || normalized !== "normal") {
                this.emotionTriggerTime = performance.now();
            }
            this.currentEmotion = normalized;
        }
    }

    /**
     * OBS・ステージ演出用の感情モーション強度（0.0〜2.0）を設定する。
     * @param {number} val モーション強度
     */
    setMotionIntensity(val) {
        this.motionIntensity = Math.max(0.0, Math.min(2.0, Number(val) || 1.0));
    }

    /**
     * 手動で口の形（通常・あ・い・う・え・お）を固定または自動解除する。
     * @param {string|null} vowel "normal" | "a" | "i" | "u" | "e" | "o" | null (自動)
     */
    setManualVowel(vowel) {
        if (vowel && VOWELS.includes(vowel)) {
            this.manualVowel = vowel;
        } else {
            this.manualVowel = null;
        }
    }

    /**
     * 目閉じ（blink）の強制表示をトグルまたは設定する。
     * @param {boolean} [force] 指定しない場合は現在の状態を反転
     * @returns {boolean} 更新後の forceBlink 状態
     */
    toggleForceBlink(force) {
        this.forceBlink = typeof force === "boolean" ? force : !this.forceBlink;
        return this.forceBlink;
    }

    /**
     * 表示モード（バストアップ / 全身）を切り替える。
     * @param {string} mode "bust" | "full"
     */
    setViewMode(mode) {
        this.viewMode = mode === "full" ? "full" : "bust";
    }

    /**
     * 発話開始時にセリフ文字列と音声ソース種別をセットする。
     * @param {string} text 発話テキスト
     * @param {string} [sourceType="webaudio"] "webaudio" | "browser"
     */
    startSpeech(text, sourceType = "webaudio") {
        const cleanText = (text || "").trim();
        this.speechVowels = this._extractVowelsFromText(cleanText);
        this.speechStartTime = performance.now();
        this.speechSource = sourceType;
        this.isSpeaking = true;
        this.lastVowel = "normal";
        this.lastVowelChangeTime = 0;
        // ブラウザTTS利用時にonend遅延で口が動き続けるのを防ぐ最大想定時間
        this.speechExpectedEndTime = this.speechStartTime + Math.min(12000, cleanText.length * 135 + 200);
    }

    /**
     * 発話を即座に終了し、口を閉じる。
     */
    stopSpeech() {
        this.isSpeaking = false;
        this.speechSource = "none";
        this.audioLevel = 0.0;
        this.smoothAudio = 0.0;
        this.formantHint = null;
        this.lastVowel = "normal";
    }

    /**
     * 現在の音量レベル（0.0〜1.0）および任意で周波数スペクトル推定母音ヒントを設定する。
     * @param {number} level 音量レベル
     * @param {string|null} [vowelHint=null] 推定母音ヒント
     */
    setAudioLevel(level, vowelHint = null) {
        this.audioLevel = Math.max(0.0, Math.min(1.0, level));
        this.formantHint = vowelHint && VOWELS.includes(vowelHint) ? vowelHint : null;
    }

    /**
     * 祝福(スパチャ)やアラート時の短時間ステージオーバーレイ演出を起動する。
     * @param {"celebrate"|"alert"|"none"} effectType
     * @param {number} [durationMs=2400]
     */
    triggerActionEffect(effectType, durationMs = 2400) {
        this.actionEffectType = effectType || "none";
        this.actionEffectUntil = performance.now() + durationMs;
    }

    /**
     * テキストから自然なリップシンク用の母音配列（["a", "e", "u", "o", "normal"]）を生成する。
     * @param {string} text 入力セリフ
     * @returns {string[]} 母音配列
     */
    _extractVowelsFromText(text) {
        const result = [];
        const naturalCycle = ["a", "e", "o", "e", "u", "a", "e"];
        for (let idx = 0; idx < text.length; idx++) {
            const ch = text[idx];
            let matched = null;
            for (const [vKey, chars] of Object.entries(KANA_LIPSYNC_MAP)) {
                if (chars.includes(ch)) {
                    matched = vKey;
                    break;
                }
            }
            if (matched) {
                result.push(matched);
            } else if (/\s/.test(ch)) {
                result.push("normal");
            } else {
                const code = ch.charCodeAt(0);
                result.push(naturalCycle[code % naturalCycle.length]);
                result.push(naturalCycle[(code + 2) % naturalCycle.length]);
            }
        }
        return result.length > 0 ? result : naturalCycle;
    }

    /**
     * 60fps メイン描画ループを開始する。
     */
    _startAnimationLoop() {
        const render = (now) => {
            this._updateState(now);
            this._drawFrame(now);
            requestAnimationFrame(render);
        };
        requestAnimationFrame(render);
    }

    /**
     * 瞬きタイミング、音量エンベロープ、補間座標を更新する。
     * @param {number} now 現在時刻(ms)
     */
    _updateState(now) {
        // 自動瞬き制御
        if (!this.isBlinking && now >= this.nextBlinkTime) {
            this.isBlinking = true;
            this.blinkEndTime = now + 120;
        } else if (this.isBlinking && now >= this.blinkEndTime) {
            this.isBlinking = false;
            if (Math.random() < 0.18) {
                this.nextBlinkTime = now + 160;
            } else {
                this.nextBlinkTime = now + 3000 + Math.random() * 3500;
            }
        }

        // 音量レベルのエンベロープ追従（無音になった瞬間に素早く0へ減衰させて口残り現象を防止）
        if (this.audioLevel < 0.04) {
            this.smoothAudio *= 0.52;
            if (this.smoothAudio < 0.01) this.smoothAudio = 0.0;
        } else if (this.audioLevel > this.smoothAudio) {
            this.smoothAudio += (this.audioLevel - this.smoothAudio) * 0.55;
        } else {
            this.smoothAudio += (this.audioLevel - this.smoothAudio) * 0.35;
        }

        // 視線・首傾げの滑らかな追従
        this.currentLookX += (this.targetLookX - this.currentLookX) * 0.05;
        this.currentLookY += (this.targetLookY - this.currentLookY) * 0.05;
    }

    /**
     * 現在の発話状態・平滑化音量・手動指定から、目の状態("open"|"blink")と母音("normal"|"a"|"i"|"u"|"e"|"o")を決定する。
     * @param {number} now 現在時刻(ms)
     * @returns {{eyeState: string, vowel: string}}
     */
    _resolveFacePattern(now) {
        const eyeState = (this.forceBlink || this.isBlinking) ? "blink" : "open";

        // 手動で口形が指定されている場合はそれを最優先で表示
        if (this.manualVowel) {
            return { eyeState, vowel: this.manualVowel };
        }

        // 1. Web Audio API (MP3/WAV) 再生時：実音声波形 (smoothAudio) のみに厳密連動
        //    音声データの末尾に無音区間があっても smoothAudio が 0 になった瞬間に即座に口を閉じる！
        if (this.speechSource === "webaudio") {
            const env = this.smoothAudio;
            if (env < 0.05) {
                this.lastVowel = "normal";
                return { eyeState, vowel: "normal" };
            }

            if (now - this.lastVowelChangeTime < 95 && this.lastVowel !== "normal") {
                return { eyeState, vowel: this.lastVowel };
            }

            let targetVowel = "normal";
            if (env < 0.16) {
                targetVowel = "u";
            } else if (env < 0.30 && !this.formantHint) {
                targetVowel = "e";
            } else if (this.formantHint && this.formantHint !== "normal") {
                targetVowel = this.formantHint;
            } else {
                const moraIndex = Math.floor((now - this.speechStartTime) / 135);
                const seqVowel = this.speechVowels.length > 0
                    ? this.speechVowels[moraIndex % this.speechVowels.length]
                    : "a";
                targetVowel = seqVowel === "normal" ? "e" : seqVowel;
            }

            if (targetVowel !== this.lastVowel) {
                this.lastVowel = targetVowel;
                this.lastVowelChangeTime = now;
            }
            return { eyeState, vowel: this.lastVowel };
        }

        // 2. ブラウザ内蔵 Web Speech API フォールバック時
        if (this.speechSource === "browser" && this.isSpeaking) {
            if (now > this.speechExpectedEndTime) {
                this.stopSpeech();
                return { eyeState, vowel: "normal" };
            }
            if (now - this.lastVowelChangeTime < 105) {
                return { eyeState, vowel: this.lastVowel };
            }
            const moraIndex = Math.floor((now - this.speechStartTime) / 135);
            const seqVowel = this.speechVowels.length > 0
                ? this.speechVowels[moraIndex % this.speechVowels.length]
                : "e";
            this.lastVowel = seqVowel;
            this.lastVowelChangeTime = now;
            return { eyeState, vowel: this.lastVowel };
        }

        this.lastVowel = "normal";
        return { eyeState, vowel: "normal" };
    }

    /**
     * 時間帯と天気に連動した柔らかな背景ライティング・星空・雨粒を描画する（OBS/マスコット透過時はスキップ）。
     */
    _drawEnvironmentBackdrop(ctx, w, h, now) {
        if (this.isObsMode || this.isMascotMode || !this.weatherBgEnabled) return;
        const period = (this.environment && this.environment.time_period) || "day";
        const weather = (this.environment && this.environment.weather_kind) || "clear";

        ctx.save();
        const grad = ctx.createRadialGradient(w * 0.5, h * 0.28, w * 0.05, w * 0.5, h * 0.5, w * 0.72);
        if (period === "morning") {
            grad.addColorStop(0, "rgba(253, 224, 71, 0.14)");
            grad.addColorStop(0.55, "rgba(56, 189, 248, 0.08)");
            grad.addColorStop(1, "rgba(15, 23, 42, 0)");
        } else if (period === "evening") {
            grad.addColorStop(0, "rgba(251, 146, 60, 0.17)");
            grad.addColorStop(0.55, "rgba(236, 72, 153, 0.09)");
            grad.addColorStop(1, "rgba(15, 23, 42, 0)");
        } else if (period === "night") {
            grad.addColorStop(0, "rgba(99, 102, 241, 0.16)");
            grad.addColorStop(0.6, "rgba(15, 23, 42, 0.10)");
            grad.addColorStop(1, "rgba(2, 6, 23, 0)");
        } else {
            grad.addColorStop(0, "rgba(56, 189, 248, 0.11)");
            grad.addColorStop(1, "rgba(15, 23, 42, 0)");
        }
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, w, h);

        // 夜間のきらめく星粒
        if (period === "night") {
            for (const st of this.stars) {
                const tw = 0.35 + 0.55 * (0.5 + 0.5 * Math.sin(now * 0.0025 + st.phase));
                ctx.fillStyle = `rgba(224, 242, 254, ${tw.toFixed(2)})`;
                ctx.beginPath();
                ctx.arc(st.x * w, st.y * h, st.size, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        // 雨天時の静かな雨粒エフェクト
        if (weather === "rain") {
            ctx.strokeStyle = "rgba(147, 197, 253, 0.34)";
            ctx.lineWidth = 1.25;
            ctx.beginPath();
            for (const rd of this.raindrops) {
                rd.y += rd.speed;
                rd.x -= rd.speed * 0.18;
                if (rd.y > 1.05) {
                    rd.y = -0.05;
                    rd.x = Math.random() * 1.15;
                }
                const rx = rd.x * w;
                const ry = rd.y * h;
                ctx.moveTo(rx, ry);
                ctx.lineTo(rx - rd.len * 0.22, ry + rd.len);
            }
            ctx.stroke();
        }
        ctx.restore();
    }

    /**
     * 装着中のアクセサリー（猫耳・メガネ・ヘッドホン・赤リボン・照れチーク）と感情記号をキャラ座標へ重ね描画する。
     */
    _drawAccessoriesAndMarks(ctx, headX, headY, headR, now) {
        const acc = new Set(this.activeAccessories || []);
        const isBlushing =
            acc.has("blush") ||
            now < this.blushUntil ||
            (this.currentEmotion === "smile" && now - this.emotionTriggerTime < 2200);

        ctx.save();
        // 1. 頬のふんわり赤面チーク
        if (isBlushing) {
            for (const side of [-1, 1]) {
                const bx = headX + side * headR * 0.44;
                const by = headY + headR * 0.26;
                const bg = ctx.createRadialGradient(bx, by, 1, bx, by, headR * 0.26);
                bg.addColorStop(0, "rgba(244, 63, 94, 0.42)");
                bg.addColorStop(1, "rgba(244, 63, 94, 0)");
                ctx.fillStyle = bg;
                ctx.beginPath();
                ctx.ellipse(bx, by, headR * 0.26, headR * 0.14, 0, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        // 2. インテリメガネ (glasses)
        if (acc.has("glasses")) {
            const eyeY = headY + headR * 0.08;
            const lensW = headR * 0.38;
            const lensH = headR * 0.21;
            const gap = headR * 0.11;
            ctx.strokeStyle = "#dc2626";
            ctx.lineWidth = Math.max(2, headR * 0.032);
            ctx.fillStyle = "rgba(224, 242, 254, 0.16)";
            for (const side of [-1, 1]) {
                const lx = side < 0 ? headX - gap - lensW : headX + gap;
                ctx.beginPath();
                ctx.roundRect(lx, eyeY - lensH / 2, lensW, lensH, headR * 0.06);
                ctx.fill();
                ctx.stroke();
            }
            ctx.beginPath();
            ctx.moveTo(headX - gap, eyeY - lensH * 0.08);
            ctx.lineTo(headX + gap, eyeY - lensH * 0.08);
            ctx.stroke();
        }

        // 3. 猫耳カチューシャ (cat_ears)
        if (acc.has("cat_ears")) {
            for (const side of [-1, 1]) {
                const baseX = headX + side * headR * 0.56;
                const baseY = headY - headR * 0.72;
                const tipX = headX + side * headR * 0.86;
                const tipY = headY - headR * 1.36;
                const outerX = headX + side * headR * 0.96;
                const outerY = headY - headR * 0.52;
                ctx.fillStyle = "#18181b";
                ctx.strokeStyle = "#f43f5e";
                ctx.lineWidth = Math.max(1.8, headR * 0.025);
                ctx.beginPath();
                ctx.moveTo(baseX, baseY);
                ctx.lineTo(tipX, tipY);
                ctx.lineTo(outerX, outerY);
                ctx.closePath();
                ctx.fill();
                ctx.stroke();

                ctx.fillStyle = "rgba(251, 113, 133, 0.85)";
                ctx.beginPath();
                ctx.moveTo(baseX + side * headR * 0.08, baseY - headR * 0.03);
                ctx.lineTo(tipX - side * headR * 0.04, tipY + headR * 0.17);
                ctx.lineTo(outerX - side * headR * 0.09, outerY - headR * 0.04);
                ctx.closePath();
                ctx.fill();
            }
        }

        // 4. ゲーミングヘッドホン (headphones)
        if (acc.has("headphones")) {
            ctx.strokeStyle = "#0f172a";
            ctx.lineWidth = Math.max(5, headR * 0.11);
            ctx.beginPath();
            ctx.arc(headX, headY - headR * 0.06, headR * 1.06, Math.PI * 1.08, Math.PI * 1.92);
            ctx.stroke();
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = Math.max(2, headR * 0.035);
            ctx.beginPath();
            ctx.arc(headX, headY - headR * 0.06, headR * 1.06, Math.PI * 1.15, Math.PI * 1.85);
            ctx.stroke();
            for (const side of [-1, 1]) {
                const ex = headX + side * headR * 1.02;
                const ey = headY + headR * 0.06;
                ctx.fillStyle = "#0f172a";
                ctx.strokeStyle = "#38bdf8";
                ctx.lineWidth = Math.max(2, headR * 0.03);
                ctx.beginPath();
                ctx.roundRect(ex - headR * 0.13, ey - headR * 0.28, headR * 0.26, headR * 0.56, headR * 0.1);
                ctx.fill();
                ctx.stroke();
            }
        }

        // 5. ツイン赤リボン (ribbon)
        if (acc.has("ribbon")) {
            for (const side of [-1, 1]) {
                const rx = headX + side * headR * 0.92;
                const ry = headY - headR * 0.56;
                ctx.fillStyle = "#e11d48";
                ctx.strokeStyle = "#fff1f2";
                ctx.lineWidth = Math.max(1.2, headR * 0.018);
                ctx.beginPath();
                ctx.moveTo(rx, ry);
                ctx.lineTo(rx + side * headR * 0.34, ry - headR * 0.2);
                ctx.lineTo(rx + side * headR * 0.32, ry + headR * 0.18);
                ctx.closePath();
                ctx.fill();
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(rx, ry, headR * 0.09, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        // 6. 感情連動の可愛い漫画記号（感情変化直後の 2.2秒間表示）
        const emoAge = now - this.emotionTriggerTime;
        if (emoAge < 2200) {
            const fade = Math.min(1, (2200 - emoAge) / 500);
            ctx.globalAlpha = fade;
            const mx = headX + headR * 1.08;
            const my = headY - headR * 0.72;
            if (this.currentEmotion === "troubled" || this.currentEmotion === "surprised") {
                // 汗マーク💦
                ctx.fillStyle = "#38bdf8";
                ctx.beginPath();
                ctx.moveTo(mx, my - headR * 0.18);
                ctx.quadraticCurveTo(mx + headR * 0.14, my + headR * 0.08, mx, my + headR * 0.16);
                ctx.quadraticCurveTo(mx - headR * 0.14, my + headR * 0.08, mx, my - headR * 0.18);
                ctx.fill();
            } else if (this.currentEmotion === "serious") {
                // 怒りマーク💢
                ctx.strokeStyle = "#f43f5e";
                ctx.lineWidth = Math.max(2.5, headR * 0.04);
                const r = headR * 0.13;
                for (let k = 0; k < 4; k++) {
                    const a = (k * Math.PI) / 2 + Math.PI / 4;
                    ctx.beginPath();
                    ctx.arc(mx + Math.cos(a) * r, my + Math.sin(a) * r, r * 0.65, a + Math.PI * 0.75, a + Math.PI * 1.25);
                    ctx.stroke();
                }
            } else if (this.currentEmotion === "joy") {
                // キラキラ星✨
                ctx.fillStyle = "#fde047";
                ctx.font = `bold ${Math.round(headR * 0.42)}px sans-serif`;
                ctx.fillText("✦", mx - headR * 0.1, my + Math.sin(now * 0.01) * 4);
            }
        }
        ctx.restore();
    }

    /**
     * 1フレームを高精細描画する（マルチ衣装・2.5D呼吸＆揺れ・アクセサリー・環境演出・ふれあい演出対応）。
     * @param {number} now 現在時刻(ms)
     */
    _drawFrame(now) {
        const ctx = this.ctx;
        const w = this.canvas.width;
        const h = this.canvas.height;
        ctx.clearRect(0, 0, w, h);

        this._drawEnvironmentBackdrop(ctx, w, h, now);

        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";

        const cx = w / 2;
        const cy = h / 2;

        const breathPhase = Math.sin(now * 0.0022);
        const breathY = breathPhase * 2.8;
        const breathScaleY = 1.0 + breathPhase * 0.0065 * this.motionIntensity;
        const breathScaleX = 1.0 - breathPhase * 0.003 * this.motionIntensity;
        let emoOffsetX = 0;
        let emoOffsetY = 0;
        let emoAngle = this.currentLookX * 0.014 * this.motionIntensity;

        // 感情変化直後（900ms間）および発話中の感情別モーション演出
        const elapsedEmo = now - this.emotionTriggerTime;
        const mi = this.motionIntensity;
        if (mi > 0.01) {
            if (elapsedEmo < 900) {
                const progress = elapsedEmo / 900.0;
                const decay = 1.0 - progress;
                if (this.currentEmotion === "joy") {
                    emoOffsetY = -Math.abs(Math.sin(progress * Math.PI * 2.2)) * 18.0 * decay * mi;
                } else if (this.currentEmotion === "surprised") {
                    emoOffsetY = -Math.sin(progress * Math.PI) * 14.0 * decay * mi;
                    emoOffsetX = Math.sin(progress * Math.PI * 8) * 3.5 * decay * mi;
                } else if (this.currentEmotion === "serious") {
                    emoOffsetX = Math.sin(progress * Math.PI * 10) * 3.0 * decay * mi;
                } else if (this.currentEmotion === "troubled") {
                    emoOffsetY = Math.sin(progress * Math.PI) * 10.0 * mi;
                    emoAngle += -0.028 * Math.sin(progress * Math.PI) * mi;
                } else if (this.currentEmotion === "smile") {
                    emoAngle += Math.sin(progress * Math.PI * 2) * 0.026 * decay * mi;
                }
            } else if (this.isSpeaking || this.smoothAudio > 0.05) {
                if (this.currentEmotion === "joy" || this.currentEmotion === "smile") {
                    emoOffsetY = Math.sin(now * 0.008) * 3.8 * mi;
                } else {
                    emoOffsetY = Math.sin(now * 0.009) * (1.8 + this.smoothAudio * 2.5) * mi;
                }
            }
        }

        const offsetX = this.currentLookX * 5.0 + emoOffsetX;
        const offsetY = breathY + this.currentLookY * 2.5 + emoOffsetY;
        const cam = this._getCurrentTransform();

        const { eyeState, vowel } = this._resolveFacePattern(now);
        const emotionGroup = this.sprites[this.currentEmotion] || this.sprites["normal"];
        const eyeGroup = emotionGroup ? emotionGroup[eyeState] : null;
        const sprite = eyeGroup ? (eyeGroup[vowel] || eyeGroup["normal"]) : null;

        const customOutfit = this.activeOutfit && this.activeOutfit !== "default" ? this.outfitImages[this.activeOutfit] : null;
        const useCustomImg =
            customOutfit &&
            (this.viewMode === "full"
                ? customOutfit.full && customOutfit.full.complete && customOutfit.full.naturalWidth > 0
                : customOutfit.bust && customOutfit.bust.complete && customOutfit.bust.naturalWidth > 0);

        ctx.save();
        ctx.translate(cx + offsetX + (cam.panX || 0), cy + offsetY + (cam.panY || 0));
        if (Math.abs(emoAngle) > 0.0005) {
            ctx.rotate(emoAngle);
        }
        const zoom = cam.zoom || 1.0;
        ctx.scale(zoom * breathScaleX, zoom * breathScaleY);

        let headX = 0;
        let headY = -h * 0.18;
        let headR = Math.min(w, h) * 0.13;

        if (useCustomImg) {
            const img = this.viewMode === "full" ? customOutfit.full : customOutfit.bust;
            const targetH = this.viewMode === "full" ? h * 0.95 : Math.min(w, h) * 0.94;
            const aspect = img.naturalWidth / img.naturalHeight;
            const drawW = targetH * aspect;
            const drawH = targetH;
            const drawX = -drawW / 2;
            const drawY = -drawH / 2 + (this.viewMode === "full" ? 6 : 12);
            ctx.drawImage(img, drawX, drawY, drawW, drawH);

            // 新衣装用の顔中心・半径を比率算出
            headX = 0;
            headY = drawY + drawH * (this.viewMode === "full" ? 0.125 : 0.22);
            headR = drawW * (this.viewMode === "full" ? 0.23 : 0.225);
        } else if (this.viewMode === "full") {
            const fullImg = this.fullSprites[this.currentEmotion] || this.fullSprites["normal"];
            if (fullImg && fullImg.complete && fullImg.naturalWidth > 0) {
                const targetH = h * 0.95;
                const aspect = fullImg.naturalWidth / fullImg.naturalHeight;
                const targetW = targetH * aspect;
                const fullX = -targetW / 2;
                const fullY = -targetH / 2 + 6;

                ctx.drawImage(fullImg, fullX, fullY, targetW, targetH);

                if ((eyeState !== "open" || vowel !== "normal") && sprite && sprite.complete && sprite.naturalWidth > 0) {
                    const sx = 265;
                    const sy = 340;
                    const sw = 310;
                    const sh = 185;
                    const fullCropX = 497.0 + sx * (701.0 / 825.0);
                    const fullCropY = 35.0 + (sy + 4.0) * (714.0 / 840.0);
                    const fullCropW = sw * (701.0 / 825.0);
                    const fullCropH = sh * (714.0 / 840.0);

                    const dx = fullX + (fullCropX / 1696.0) * targetW;
                    const dy = fullY + (fullCropY / 2528.0) * targetH;
                    const dw = (fullCropW / 1696.0) * targetW;
                    const dh = (fullCropH / 2528.0) * targetH;

                    ctx.drawImage(sprite, sx, sy, sw, sh, dx, dy, dw, dh);
                }
                headX = 0;
                headY = fullY + targetH * 0.135;
                headR = targetW * 0.165;
            }
        } else {
            if (sprite && sprite.complete && sprite.naturalWidth > 0) {
                const targetH = Math.min(w, h) * 0.94;
                const aspect = sprite.naturalWidth / sprite.naturalHeight;
                const drawW = targetH * aspect;
                const drawH = targetH;
                const drawY = -drawH / 2 + 12;

                ctx.drawImage(sprite, -drawW / 2, drawY, drawW, drawH);
                headX = 0;
                headY = drawY + drawH * 0.45;
                headR = drawW * 0.185;
            }
        }

        this._drawAccessoriesAndMarks(ctx, headX, headY, headR, now);
        ctx.restore();

        // なでなで・つつき時のハート＆キラキラ粒子描画
        if (this.hearts.length > 0) {
            ctx.save();
            const nextHearts = [];
            for (const p of this.hearts) {
                const age = now - p.born;
                if (age >= p.life) continue;
                p.x += p.vx;
                p.y += p.vy;
                const alpha = Math.max(0, 1 - age / p.life);
                ctx.globalAlpha = alpha;
                ctx.font = p.kind === "heart" ? "bold 22px sans-serif" : "bold 18px sans-serif";
                ctx.fillStyle = p.kind === "heart" ? "#fb7185" : "#fde047";
                ctx.fillText(p.kind === "heart" ? "♥" : "✦", p.x, p.y);
                nextHearts.push(p);
            }
            this.hearts = nextHearts;
            ctx.restore();
        }

        // 着替え変身時のキラキラパーティクルエフェクト
        if (now < this.outfitChangeUntil) {
            const remain = (this.outfitChangeUntil - now) / 1150.0;
            ctx.save();
            for (let i = 0; i < 20; i++) {
                const ang = (i / 20) * Math.PI * 2 + now * 0.004;
                const rad = (0.14 + (1 - remain) * 0.34) * Math.min(w, h);
                const px = cx + Math.cos(ang) * rad;
                const py = cy * 0.92 + Math.sin(ang) * rad * 0.85;
                ctx.fillStyle = i % 2 === 0
                    ? `rgba(244, 114, 182, ${(remain * 0.9).toFixed(2)})`
                    : `rgba(253, 224, 71, ${(remain * 0.9).toFixed(2)})`;
                ctx.font = `bold ${14 + (i % 3) * 4}px sans-serif`;
                ctx.fillText("✦", px, py);
            }
            ctx.restore();
        }

        // スパチャ受信時・お祝い時のキラキラパーティクルエフェクト
        if (this.actionEffectType === "celebrate" && now < this.actionEffectUntil) {
            const remain = (this.actionEffectUntil - now) / 2400.0;
            ctx.save();
            for (let i = 0; i < 16; i++) {
                const angle = (i / 16) * Math.PI * 2 + now * 0.002;
                const radius = (0.30 + (1.0 - remain) * 0.16) * Math.min(w, h);
                const px = cx + Math.cos(angle) * radius;
                const py = cy * 0.85 + Math.sin(angle) * (radius * 0.72) - (1.0 - remain) * 45;
                ctx.fillStyle = i % 2 === 0
                    ? `rgba(251, 191, 36, ${(remain * 0.85).toFixed(2)})`
                    : `rgba(56, 189, 248, ${(remain * 0.85).toFixed(2)})`;
                ctx.beginPath();
                ctx.arc(px, py, 3.5 + (i % 3), 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.restore();
        }
    }
}
