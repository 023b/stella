// Stella: a top-bar assistant behind the Copilot key.
//
// The Copilot key on recent laptops sends Super+Shift+F23 (xkb maps it to XF86Assistant at that level).
//   tap          open the typing card, tap again to close it
//   press, hold  push to talk: records while held, release sends
// The key keeps Super+Shift down while pressed, so the hold is read from those modifiers.
//
// Speech in and out goes through the local helper (helper/stella_helper.py, 127.0.0.1:8098).
// Every question goes to one n8n webhook, which decides what to do and answers {say, pending, action}.
// Settings: ~/.config/stella/config.json  {"n8n": "...", "helper": "...", "speak": true, "voiceInput": true}

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const W = 420;             // card width
const GAP = 8;             // card sits this far below the bar
const LONG_MS = 320;       // held this long = push to talk, shorter = tap
const CLOSE_AFTER = 8000;  // voice card closes this long after the answer
const ANSWER_WAIT = 20000; // card stays this long when Stella asked something back
const KEYS = ['<Super><Shift>XF86Assistant', '<Super><Shift>F23', '<Super><Shift>XF86TouchpadOff', 'XF86Assistant'];
const CONF_DIR = GLib.build_filenamev([GLib.get_user_config_dir(), 'stella']);
const DEFAULTS = {
    n8n: 'http://127.0.0.1:5678/webhook/stella/voice',
    helper: 'http://127.0.0.1:8098',
    speak: true,
    voiceInput: true,
};
const enc = new TextEncoder();
const dec = new TextDecoder();

export default class StellaExtension extends Extension {
    enable() {
        this._stella = new Stella(this.path);
        this._stella.enable();
    }

    disable() {
        this._stella?.disable();
        this._stella = null;
    }
}

class Stella {
    constructor(path) {
        this._icon = Gio.FileIcon.new(Gio.File.new_for_path(`${path}/icons/stella-symbolic.svg`));
    }

    enable() {
        this._soup = new Soup.Session({timeout: 0});
        this._cancel = new Gio.Cancellable();
        this._mode = null;      // null (closed), 'text' or 'voice'
        this._pending = null;   // follow-up context from the last reply
        this._turn = 0;         // bumps on open/close so stale replies are dropped
        this._grab = null;
        this._hold = null;      // push-to-talk id while a hold is recording
        this._conf = this._load();
        this._buildMark();
        this._buildCard();
        this._buildMenu();
        this._bindKeys();
    }

    disable() {
        this._close(true);
        this._cancel.cancel();
        this._soup.abort();
        this._endHold(false);
        for (const id of ['_pressId', '_closeId', '_keyPollId'])
            if (this[id])
                GLib.source_remove(this[id]);
        global.display.disconnect(this._accelId);
        for (const a of this._actions) {
            global.display.ungrab_accelerator(a);
            Main.wm.allowKeybinding(Meta.external_binding_name_for_action(a), Shell.ActionMode.NONE);
        }
        this._menu.destroy();
        Main.layoutManager.removeChrome(this._card);
        this._card.destroy();
        this._ind.destroy();
        this._card = this._ind = this._soup = null;
    }

    // ---------- settings ----------

    _load() {
        try {
            const [, b] = Gio.File.new_for_path(`${CONF_DIR}/config.json`).load_contents(null);
            return {...DEFAULTS, ...JSON.parse(dec.decode(b))};
        } catch {
            return {...DEFAULTS};
        }
    }

    _save() {
        GLib.mkdir_with_parents(CONF_DIR, 0o700);
        Gio.File.new_for_path(`${CONF_DIR}/config.json`).replace_contents(
            enc.encode(JSON.stringify(this._conf, null, 2)), null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
    }

    _helper(path, body, timeoutS) {
        return this._post(`${this._conf.helper}${path}`, body, timeoutS);
    }

    // ---------- top-bar mark ----------

    _buildMark() {
        this._mark = new St.Icon({gicon: this._icon, style_class: 'st-mark', y_align: Clutter.ActorAlign.CENTER});
        this._ind = new St.Bin({
            style_class: 'st-ind',
            reactive: true,
            track_hover: true,
            y_align: Clutter.ActorAlign.CENTER,
            child: this._mark,
        });
        this._ind.connect('button-press-event', (_a, e) => {
            if (e.get_button() === 3) {
                this._menu.toggle();
                return Clutter.EVENT_STOP;
            }
            if (e.get_button() !== 1)
                return Clutter.EVENT_PROPAGATE;
            this._pressStart();
            return Clutter.EVENT_STOP;
        });
        this._ind.connect('button-release-event', (_a, e) => {
            if (e.get_button() !== 1)
                return Clutter.EVENT_PROPAGATE;
            this._pressEnd();
            return Clutter.EVENT_STOP;
        });
        Main.panel._centerBox.add_child(this._ind);
    }

    // ---------- right-click menu ----------

    _buildMenu() {
        this._menu = new PopupMenu.PopupMenu(this._ind, 0.5, St.Side.TOP);
        Main.uiGroup.add_child(this._menu.actor);
        this._menu.actor.hide();
        Main.panel.menuManager.addMenu(this._menu);

        this._inputItem = new PopupMenu.PopupSwitchMenuItem('Hold to talk', this._conf.voiceInput);
        this._inputItem.connect('toggled', (_i, on) => {
            this._conf.voiceInput = on;
            this._save();
        });
        this._menu.addMenuItem(this._inputItem);

        this._speakItem = new PopupMenu.PopupSwitchMenuItem('Speak replies', this._conf.speak);
        this._speakItem.connect('toggled', (_i, on) => this._setSpeak(on));
        this._menu.addMenuItem(this._speakItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const stop = new PopupMenu.PopupMenuItem('Stop talking');
        stop.connect('activate', () => this._helper('/hush', {}, 3).catch(() => {}));
        this._menu.addMenuItem(stop);
    }

    _setSpeak(on) {
        this._conf.speak = on;
        this._save();
        if (!on)
            this._helper('/hush', {}, 3).catch(() => {});
        if (this._speakBtn.checked !== on)
            this._speakBtn.checked = on;
        this._speakItem.setToggleState(on);
    }

    // ---------- the card ----------

    _buildCard() {
        this._card = new St.Bin({style_class: 'st-card', reactive: true, visible: false});
        this._content = new St.BoxLayout({style_class: 'st-content', orientation: Clutter.Orientation.VERTICAL, width: W});
        this._card.set_child(this._content);

        const head = new St.BoxLayout({style_class: 'st-head'});
        this._state = new St.Label({style_class: 'st-state', x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._speakIcon = new St.Icon({icon_name: 'audio-volume-muted-symbolic'});
        this._speakBtn = new St.Button({style_class: 'st-btn', toggle_mode: true, child: this._speakIcon,
            y_align: Clutter.ActorAlign.CENTER});
        this._speakBtn.connect('notify::checked', () => {
            const on = this._speakBtn.checked;
            this._speakIcon.icon_name = on ? 'audio-volume-high-symbolic' : 'audio-volume-muted-symbolic';
            if (on !== this._conf.speak)
                this._setSpeak(on);
        });
        this._speakBtn.checked = this._conf.speak;
        const mic = new St.Button({style_class: 'st-btn', y_align: Clutter.ActorAlign.CENTER,
            child: new St.Icon({icon_name: 'audio-input-microphone-symbolic'})});
        mic.connect('clicked', () => this._switch('voice'));   // hands-free turn, stops on silence
        head.add_child(this._state);
        head.add_child(this._speakBtn);
        head.add_child(mic);
        this._content.add_child(head);

        this._entry = new St.Entry({style_class: 'st-entry', hint_text: 'Ask Stella', can_focus: true, x_expand: true});
        this._entry.clutter_text.connect('activate', () => this._sendText());
        this._entry.clutter_text.connect('key-press-event', (_a, e) => {
            if (e.get_key_symbol() === Clutter.KEY_Escape) {
                this._close();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this._content.add_child(this._entry);

        const wrap = l => {
            l.clutter_text.set({line_wrap: true, line_wrap_mode: Pango.WrapMode.WORD_CHAR, ellipsize: Pango.EllipsizeMode.NONE});
            return l;
        };
        this._heard = wrap(new St.Label({style_class: 'st-heard', visible: false}));
        this._say = wrap(new St.Label({style_class: 'st-say', visible: false}));
        this._content.add_child(this._heard);
        this._content.add_child(this._say);

        Main.layoutManager.addTopChrome(this._card);
    }

    _setState(mark, text) {
        for (const st of ['listening', 'thinking', 'answer'])
            this._ind.remove_style_class_name(st);
        if (mark !== 'idle')
            this._ind.add_style_class_name(mark);
        this._state.text = text;
    }

    _show(label, text) {
        label.text = text || '';
        label.visible = !!text;
    }

    _place() {
        const [ix] = this._ind.get_transformed_position();
        const [iw] = this._ind.get_transformed_size();
        const mon = Main.layoutManager.findMonitorForActor(this._ind) ?? Main.layoutManager.primaryMonitor;
        const panel = Main.panel.get_transformed_extents();
        const w = W + 2;
        let x = Math.round(ix + iw / 2 - w / 2);
        x = Math.max(mon.x + 8, Math.min(x, mon.x + mon.width - w - 8));
        this._card.set_position(x, Math.round(panel.origin.y + panel.size.height + GAP));
    }

    _open(mode, hold = null) {
        this._turn++;
        this._pending = null;
        this._clearClose();
        this._show(this._heard, '');
        this._show(this._say, '');
        this._entry.text = '';
        this._entry.hint_text = 'Ask Stella';
        this._place();
        this._card.remove_all_transitions();
        this._card.set({opacity: 0, visible: true});
        this._card.ease({opacity: 255, duration: 140, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._mode = null;
        this._opening = true;
        this._switch(mode, hold);
    }

    _switch(mode, hold = null) {
        const opening = this._opening;
        this._opening = false;
        if (!opening && (!this._card.visible || !this._mode))
            return this._open(mode, hold);
        const was = this._mode;
        this._mode = mode;
        this._clearClose();
        if (mode === 'text') {
            this._entry.show();
            this._setState('idle', 'Type');
            this._modal(true);
            this._entry.grab_key_focus();
        } else {
            if (was === 'text')
                this._modal(false);
            this._entry.hide();
            this._voiceTurn(hold);
        }
    }

    _modal(on) {
        if (on && !this._grab) {
            this._grab = Main.pushModal(this._card, {actionMode: Shell.ActionMode.POPUP});
            this._capId = global.stage.connect('captured-event', (_s, e) => {
                const t = e.type();
                if (t === Clutter.EventType.BUTTON_RELEASE && this._pressId) {
                    this._pressEnd();
                    return Clutter.EVENT_STOP;
                }
                if (t === Clutter.EventType.KEY_PRESS && e.get_key_symbol() === Clutter.KEY_Escape) {
                    this._close();
                    return Clutter.EVENT_STOP;
                }
                if (t !== Clutter.EventType.BUTTON_PRESS && t !== Clutter.EventType.TOUCH_BEGIN)
                    return Clutter.EVENT_PROPAGATE;
                const [x, y] = e.get_coords();
                const inside = a => {
                    const [ax, ay] = a.get_transformed_position();
                    const [aw, ah] = a.get_transformed_size();
                    return x >= ax && x <= ax + aw && y >= ay && y <= ay + ah;
                };
                if (inside(this._card))
                    return Clutter.EVENT_PROPAGATE;
                if (inside(this._ind) && e.get_button() === 1) {
                    this._pressStart();
                    return Clutter.EVENT_STOP;
                }
                this._close();
                return Clutter.EVENT_STOP;
            });
        } else if (!on && this._grab) {
            global.stage.disconnect(this._capId);
            Main.popModal(this._grab);
            this._grab = null;
        }
    }

    _close(now = false, byHand = true) {
        if (!this._card)
            return;
        if (byHand && this._mode)
            this._helper('/hush', {}, 3).catch(() => {});
        this._turn++;
        this._clearClose();
        this._endHold(false);
        if (this._mode === 'voice')
            this._helper('/cancel', {}, 5).catch(() => {});
        this._modal(false);
        this._mode = null;
        this._pending = null;
        this._setState('idle', '');
        if (now || !this._card.visible) {
            this._card.hide();
            return;
        }
        this._card.ease({
            opacity: 0, duration: 120, mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => {
                if (!this._mode)
                    this._card.hide();
            },
        });
    }

    _closeLater(ms = CLOSE_AFTER) {
        this._clearClose();
        this._closeId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._closeId = 0;
            this._close(false, false);
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearClose() {
        if (this._closeId) {
            GLib.source_remove(this._closeId);
            this._closeId = 0;
        }
    }

    // ---------- talking to n8n and the helper ----------

    _post(url, obj, timeoutS) {
        return new Promise((resolve, reject) => {
            const msg = Soup.Message.new('POST', url);
            msg.set_request_body_from_bytes('application/json', new GLib.Bytes(enc.encode(JSON.stringify(obj))));
            const c = new Gio.Cancellable();
            const link = this._cancel.connect(() => c.cancel());
            let tid = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, timeoutS, () => {
                tid = 0;
                c.cancel();
                return GLib.SOURCE_REMOVE;
            });
            this._soup.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, c, (s, res) => {
                if (tid)
                    GLib.source_remove(tid);
                this._cancel.disconnect(link);
                try {
                    const bytes = s.send_and_read_finish(res);
                    if (msg.get_status() !== Soup.Status.OK)
                        throw new Error(`HTTP ${msg.get_status()}`);
                    resolve(JSON.parse(dec.decode(bytes.get_data())));
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    _sendText() {
        const text = this._entry.text.trim();
        if (!text)
            return;
        this._entry.text = '';
        this._show(this._heard, text);
        this._ask(text, false);
    }

    // hold = push-to-talk id (records until released), null = hands-free (stops on silence)
    async _voiceTurn(hold = null) {
        const turn = this._turn;
        this._setState('listening', hold ? 'Listening, release to send' : 'Listening');
        this._show(this._say, '');
        let r;
        try {
            r = await this._helper('/listen', hold ? {hold: true, id: hold} : {max_s: 12}, hold ? 40 : 25);
        } catch {
            if (turn !== this._turn)
                return;
            this._setState('idle', 'Mic helper is not running (stella-helper.service)');
            this._closeLater(4000);
            return;
        }
        if (turn !== this._turn || this._mode !== 'voice')
            return;
        if (!r.text) {
            this._setState('idle', r.speech ? "Didn't catch that" : 'Heard nothing');
            this._closeLater(this._pending ? ANSWER_WAIT : 3000);
            return;
        }
        this._show(this._heard, r.text);
        this._ask(r.text, true);
    }

    async _ask(text, voice) {
        const turn = this._turn;
        this._setState('thinking', 'Thinking');
        let r;
        try {
            r = await this._post(this._conf.n8n, {text, pending: this._pending}, 120);
        } catch {
            if (turn !== this._turn)
                return;
            this._setState('idle', "No answer from n8n. Is the workflow active?");
            if (voice)
                this._closeLater(4000);
            return;
        }
        if (turn !== this._turn || !this._mode)
            return;
        let say = r.say || '';
        if (r.action)
            say = this._act(r.action) ?? say;
        const more = !!r.pending;
        this._pending = r.pending || null;
        this._show(this._say, say || (more ? '' : 'Done.'));
        if (!voice) {
            this._setState('answer', more ? 'Waiting for your answer' : 'Stella');
            this._entry.hint_text = more ? 'Answer Stella' : 'Ask Stella';
        } else {
            this._setState('answer', more ? 'Hold to answer' : 'Stella');
        }
        if (say && this._conf.speak) {
            this._clearClose();
            try {
                await this._helper('/speak', {text: say, wait: true}, 180);
            } catch {}
            if (turn !== this._turn || !this._mode)
                return;
        }
        if (voice)
            this._closeLater(more ? ANSWER_WAIT : say && this._conf.speak ? 2500 : Math.max(CLOSE_AFTER, 70 * say.length));
    }

    // ---------- desktop actions asked for by the flow ----------
    // {type: 'open'|'close', target} or {type: 'lock'}. Returns a replacement reply when it fails.

    _act(a) {
        try {
            if (a.type === 'lock') {
                Main.screenShield?.lock(true);
                return null;
            }
            const target = String(a.target || '').trim().toLowerCase();
            if (!target)
                return null;
            if (a.type === 'open') {
                if (/^(https?:\/\/|www\.)|\.(com|org|net|io|dev|uk|ai)(\/|$)/.test(target)) {
                    const uri = target.startsWith('http') ? target : `https://${target}`;
                    Gio.AppInfo.launch_default_for_uri(uri, null);
                    return null;
                }
                const app = this._findApp(target);
                if (!app)
                    return `I couldn't find an app called ${target}.`;
                app.activate();
                return null;
            }
            if (a.type === 'close') {
                const app = this._findApp(target);
                const wins = app?.get_windows() ?? [];
                if (!wins.length)
                    return `${target} isn't open.`;
                for (const w of wins)
                    w.delete(global.get_current_time());
                return null;
            }
        } catch (e) {
            console.log(`[stella] action failed: ${e}`);
            return "That didn't work.";
        }
        return null;
    }

    _findApp(name) {
        const sys = Shell.AppSystem.get_default();
        const hits = Shell.AppSystem.search(name).flat();
        for (const id of hits) {
            const app = sys.lookup_app(id);
            if (app)
                return app;
        }
        return null;
    }

    // ---------- tap and hold (the mark and the Copilot key share this) ----------

    _pressStart() {
        if (this._pressId || this._hold)
            return;
        this._pressId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, LONG_MS, () => {
            this._pressId = 0;
            this._startHold();
            return GLib.SOURCE_REMOVE;
        });
    }

    _pressEnd() {
        if (this._pressId) {               // short: a tap toggles the typing card
            GLib.source_remove(this._pressId);
            this._pressId = 0;
            const now = GLib.get_monotonic_time();     // the Copilot key can bounce: one toggle per 300 ms
            if (now - (this._lastTap ?? 0) < 300000)
                return;
            this._lastTap = now;
            if (this._mode)
                this._close();
            else
                this._open('text');
            return;
        }
        this._endHold(true);
    }

    _startHold() {
        if (!this._conf.voiceInput) {      // voice is off: a hold is just a tap
            if (this._mode)
                this._close();
            else
                this._open('text');
            return;
        }
        this._hold = `${GLib.get_monotonic_time()}`;
        this._clearClose();
        this._helper('/hush', {}, 3).catch(() => {});
        if (this._mode === 'text')
            this._modal(false);
        if (this._card.visible && this._mode)
            this._turn++;                 // drop any reply still on its way, keep the follow-up context
        this._switch('voice', this._hold);
        // the release may land anywhere (the pointer can leave the mark while held)
        this._relId = global.stage.connect('captured-event', (_s, e) => {
            if (e.type() === Clutter.EventType.BUTTON_RELEASE && e.get_button() === 1 && !this._keyPollId)
                this._endHold(true);
            return Clutter.EVENT_PROPAGATE;
        });
    }

    _endHold(send) {
        if (this._relId) {
            global.stage.disconnect(this._relId);
            this._relId = 0;
        }
        const id = this._hold;
        if (!id)
            return;
        this._hold = null;
        this._helper(send ? '/stop' : '/cancel', {id}, 5).catch(() => {});
        if (send && this._mode === 'voice')
            this._setState('thinking', 'Transcribing');
    }

    // ---------- Copilot key ----------

    _bindKeys() {
        this._actions = [];
        for (const k of KEYS) {
            const a = global.display.grab_accelerator(k, Meta.KeyBindingFlags.NONE);
            if (a === Meta.KeyBindingAction.NONE)
                continue;
            Main.wm.allowKeybinding(Meta.external_binding_name_for_action(a),
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP);
            this._actions.push(a);
        }
        console.log(`[stella] Copilot key grabs ${this._actions.length}/${KEYS.length}`);
        this._accelId = global.display.connect('accelerator-activated', (_d, action) => {
            if (this._actions.includes(action))
                this._onKey();
        });
    }

    // The accelerator only reports the press. The Copilot key holds Super+Shift down for as long
    // as it is pressed, so watch those modifiers to tell a tap from a hold and to catch the release.
    _onKey() {
        if (this._keyPollId)
            return;                       // key repeat while held
        const HELD = Clutter.ModifierType.SUPER_MASK | Clutter.ModifierType.MOD4_MASK | Clutter.ModifierType.SHIFT_MASK;
        const t0 = GLib.get_monotonic_time();
        this._pressStart();
        this._keyPollId = GLib.timeout_add(GLib.PRIORITY_HIGH, 30, () => {
            const [, , mods] = global.get_pointer();
            if ((mods & HELD) && GLib.get_monotonic_time() - t0 < 30e6)
                return GLib.SOURCE_CONTINUE;
            this._keyPollId = 0;
            this._pressEnd();
            return GLib.SOURCE_REMOVE;
        });
    }
}
