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
            this.canvas.style.cursor = "grabbing";
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
            this.adjustPan(dx, dy);
        });

        window.addEventListener("mouseup", () => {
            if (this._isDragging) {
                this._isDragging = false;
                this.canvas.style.cursor = "grab";
            }
        });
        this.canvas.style.cursor = "grab";
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
     * 1フレームを高精細描画する（バストアップ・全身表示の両方で瞬き＆リップシンクに対応）。
     * @param {number} now 現在時刻(ms)
     */
    _drawFrame(now) {
        const ctx = this.ctx;
        const w = this.canvas.width;
        const h = this.canvas.height;
        ctx.clearRect(0, 0, w, h);

        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";

        const cx = w / 2;
        const cy = h / 2;

        const breathY = Math.sin(now * 0.002) * 2.5;
        let emoOffsetX = 0;
        let emoOffsetY = 0;
        let emoAngle = 0;

        // 感情変化直後（900ms間）および発話中の感情別モーション演出
        const elapsedEmo = now - this.emotionTriggerTime;
        const mi = this.motionIntensity;
        if (mi > 0.01) {
            if (elapsedEmo < 900) {
                const progress = elapsedEmo / 900.0;
                const decay = 1.0 - progress;
                if (this.currentEmotion === "joy") {
                    // 嬉しそうに2回弾むジャンプ
                    emoOffsetY = -Math.abs(Math.sin(progress * Math.PI * 2.2)) * 18.0 * decay * mi;
                } else if (this.currentEmotion === "surprised") {
                    // ビクッと跳ねるショック
                    emoOffsetY = -Math.sin(progress * Math.PI) * 14.0 * decay * mi;
                    emoOffsetX = Math.sin(progress * Math.PI * 8) * 3.5 * decay * mi;
                } else if (this.currentEmotion === "serious") {
                    // 鋭い微振動
                    emoOffsetX = Math.sin(progress * Math.PI * 10) * 3.0 * decay * mi;
                } else if (this.currentEmotion === "troubled") {
                    // しょんぼり沈み込む
                    emoOffsetY = Math.sin(progress * Math.PI) * 10.0 * mi;
                    emoAngle = -0.02 * Math.sin(progress * Math.PI) * mi;
                } else if (this.currentEmotion === "smile") {
                    // 柔らかい横スウェイ
                    emoAngle = Math.sin(progress * Math.PI * 2) * 0.022 * decay * mi;
                }
            } else if (this.isSpeaking || this.smoothAudio > 0.05) {
                if (this.currentEmotion === "joy" || this.currentEmotion === "smile") {
                    emoOffsetY = Math.sin(now * 0.008) * 3.5 * mi;
                }
            }
        }

        const offsetX = this.currentLookX * 3.5 + emoOffsetX;
        const offsetY = breathY + this.currentLookY * 2.0 + emoOffsetY;
        const cam = this._getCurrentTransform();

        const { eyeState, vowel } = this._resolveFacePattern(now);
        const emotionGroup = this.sprites[this.currentEmotion] || this.sprites["normal"];
        const eyeGroup = emotionGroup ? emotionGroup[eyeState] : null;
        const sprite = eyeGroup ? (eyeGroup[vowel] || eyeGroup["normal"]) : null;

        ctx.save();
        ctx.translate(cx + offsetX + (cam.panX || 0), cy + offsetY + (cam.panY || 0));
        if (Math.abs(emoAngle) > 0.0005) {
            ctx.rotate(emoAngle);
        }
        const zoom = cam.zoom || 1.0;
        if (Math.abs(zoom - 1.0) > 0.001) {
            ctx.scale(zoom, zoom);
        }

        if (this.viewMode === "full") {
            const fullImg = this.fullSprites[this.currentEmotion] || this.fullSprites["normal"];
            if (fullImg && fullImg.complete && fullImg.naturalWidth > 0) {
                const targetH = h * 0.95;
                const aspect = fullImg.naturalWidth / fullImg.naturalHeight;
                const targetW = targetH * aspect;
                const fullX = -targetW / 2;
                const fullY = -targetH / 2 + 6;

                // 1. 全身立ち絵ベースを描画
                ctx.drawImage(fullImg, fullX, fullY, targetW, targetH);

                // 2. 瞬き中または発話（口パク）中は、バストアップスプライトの顔領域 (sx:265..575, sy:340..525) を
                //    全身立ち絵の対応座標へ正確にオーバーレイ描画して全身でも瞬き＆口パクさせる
                if ((eyeState !== "open" || vowel !== "normal") && sprite && sprite.complete && sprite.naturalWidth > 0) {
                    const sx = 265;
                    const sy = 340;
                    const sw = 310;
                    const sh = 185;
                    // 825x840 バストアップ座標から 1696x2528 全身座標への厳密変換
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
            }
        } else {
            if (sprite && sprite.complete && sprite.naturalWidth > 0) {
                const targetH = Math.min(w, h) * 0.94;
                const aspect = sprite.naturalWidth / sprite.naturalHeight;
                const drawW = targetH * aspect;
                const drawH = targetH;

                ctx.drawImage(sprite, -drawW / 2, -drawH / 2 + 12, drawW, drawH);
            }
        }

        ctx.restore();

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
