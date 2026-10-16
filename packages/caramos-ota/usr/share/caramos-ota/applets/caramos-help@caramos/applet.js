// CaramOS Feedback & Help: the question mark at the right end of the panel. Its menu opens Feedback & Help
// (caramos-help) to ask the VNLF community for help, report a bug or send feedback, opens Driver Check, and
// lists the requests VNLF accepted (kept by caramos-help and Driver Check in
// ~/.local/state/caramos-help/requests.json) with a link to each request's page, where VNLF answers, what
// VNLF says about it and what the community does with it. Now and then caramos-help --refresh
// asks VNLF how the requests are doing; the window lists them all (caramos-help --requests).

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const St = imports.gi.St;
const Clutter = imports.gi.Clutter;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const Util = imports.misc.util;

// The texts are in the caramos-ota catalog, together with the Feedback & Help window.
const Gettext = imports.gettext;
const TEXT_DOMAIN = 'caramos-ota';
Gettext.bindtextdomain(TEXT_DOMAIN, '/usr/share/locale');

function _(text) {
    return Gettext.dgettext(TEXT_DOMAIN, text);
}

function ngettext(singular, plural, count) {
    return Gettext.dngettext(TEXT_DOMAIN, singular, plural, count);
}

// The icon theme's question mark, drawn like the other panel icons.
const ICON = 'dialog-question-symbolic';
const REQUESTS_SHOWN = 5;
const MESSAGE_CHARS = 48;
// VNLF's word on a request, from caramos-help --refresh (helpdesk.py); GONE: VNLF no longer has it.
const FINISHED = ['RESOLVED', 'CLOSED', 'GONE'];
const KEEP_FINISHED_DAYS = 30;   // as helpdesk.KEEP_FINISHED_DAYS: then only the window lists it
const REFRESH_FIRST_SECONDS = 60;   // after login, once the network is up
const REFRESH_EVERY_SECONDS = 30 * 60;

function requestsFile() {
    const state = GLib.getenv('XDG_STATE_HOME') || GLib.build_filenamev([GLib.get_home_dir(), '.local', 'state']);
    return GLib.build_filenamev([state, 'caramos-help', 'requests.json']);
}

// Newest first, as caramos-help writes them; only entries with a https page.
function parseRequests(text) {
    let data;
    try {
        data = JSON.parse(text);
    } catch (e) {
        return [];
    }
    const items = data && Array.isArray(data.requests) ? data.requests : [];
    return items.filter(item => item && typeof item.url === 'string' && item.url.startsWith('https://'));
}

function kindName(kind) {
    switch (kind) {
    case 'driver': return _('Driver help');
    case 'bug': return _('Bug report');
    case 'feedback': return _('Feedback');
    default: return _('Help');
    }
}

function shortDate(iso) {
    const date = new Date(iso);
    return isNaN(date.getTime()) ? '' : `${date.getDate()}/${date.getMonth() + 1}`;
}

function shorten(text) {
    const flat = String(text || '').replace(/\s+/g, ' ').trim();
    return flat.length > MESSAGE_CHARS ? `${flat.slice(0, MESSAGE_CHARS - 1)}…` : flat;
}

function finished(request) {
    return FINISHED.includes(request.status);
}

// helpdesk.listed: a request VNLF finished more than KEEP_FINISHED_DAYS ago leaves the menu.
function listed(request, now) {
    if (!finished(request)) return true;
    const when = new Date(request.finished).getTime();
    return isNaN(when) || now - when <= KEEP_FINISHED_DAYS * 24 * 3600 * 1000;
}

// In the words of the window's list (ui/requests.py); nothing while it is open and not answered.
function statusOf(request) {
    if (request.status === 'GONE') return ['action-unavailable-symbolic', _('No longer on VNLF')];
    if (request.replied === true) return ['mail-replied-symbolic', _('VNLF answered')];
    switch (request.status) {
    case 'IN_REVIEW': return ['view-reveal-symbolic', _('VNLF is looking at it')];
    case 'RESOLVED': return ['emblem-ok-symbolic', _('Resolved')];
    case 'CLOSED': return ['window-close-symbolic', _('Closed')];
    default: return null;
    }
}

// An icon, a title and a dim line under it (and a note under that), as one row: Cinnamon measures the
// children of menu items as columns shared by the whole menu, which squeezes two lines of text into one.
function twoLines(icon, title, detail, note) {
    const row = new St.BoxLayout({ style_class: 'caramos-help-row' });
    icon.add_style_class_name('popup-menu-icon');
    row.add_child(icon);
    const texts = new St.BoxLayout({ vertical: true, style_class: 'caramos-help-texts' });
    const label = new St.Label({ text: title, style_class: 'caramos-help-title' });
    texts.add_child(label);
    if (detail) texts.add_child(new St.Label({ text: detail, style_class: 'caramos-help-detail', opacity: 175 }));
    if (note) texts.add_child(note);
    row.add_child(texts);
    return [row, label];
}

function chip(iconName, text, strong) {
    const box = new St.BoxLayout({ style_class: 'caramos-help-chip', opacity: strong ? 255 : 175 });
    box.add_child(new St.Icon({ icon_name: iconName, icon_type: St.IconType.SYMBOLIC,
        style_class: 'caramos-help-note-icon', y_align: Clutter.ActorAlign.CENTER }));
    box.add_child(new St.Label({ text: text, y_align: Clutter.ActorAlign.CENTER }));
    return box;
}

// Under a request: what VNLF says about it and what the community does with it, with the theme's icons so
// it does not read as more of the message. Answers to a request that is not finished stand out.
function noteFor(request) {
    const status = statusOf(request);
    // Answers are on the public page (docs 52), so a request with answers is shared: they take its place.
    const answers = Number.isInteger(request.answers) && request.answers > 0 ? request.answers : 0;
    if (!status && !answers && request.public !== true) return null;
    const note = new St.BoxLayout({ style_class: 'caramos-help-note' });
    if (status) note.add_child(chip(status[0], status[1], request.replied === true && !finished(request)));
    if (answers) {
        const text = ngettext('%d answer from the community', '%d answers from the community', answers);
        note.add_child(chip('system-users-symbolic', text.replace('%d', String(answers)), !finished(request)));
    } else if (request.public === true) {
        // caramos-help keeps "public" when the user ticked "Let the VNLF community see it and help".
        note.add_child(chip('system-users-symbolic', _('Shared with the community')));
    }
    return note;
}

class TwoLineMenuItem extends PopupMenu.PopupBaseMenuItem {
    _init(icon, title, detail, note, params) {
        super._init(params);
        const [row, label] = twoLines(icon, title, detail, note);
        this.row = row;
        this.label = label;
        this.actor.label_actor = label;
        this.addActor(row, { span: -1, expand: true });
    }
}

class CaramOSHelpApplet extends Applet.IconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this._path = metadata.path;
        this._removed = false;
        this.set_applet_icon_symbolic_name(ICON);
        this.set_applet_tooltip(_('Feedback & Help'));
        this.actor.add_style_class_name('caramos-help-panel-button');

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menu.actor.add_style_class_name('caramos-help-popup');   // rounded like the Control Center
        this.menuManager.addMenu(this.menu);

        this.menu.addActor(this._header());
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addAction(this._themed('help-browser-symbolic'), _('Ask for help'),
            _('Something does not work and you need advice'), ['caramos-help', '--type', 'os']);
        this._addAction(this._symbolic('caramos-bug-symbolic.svg'), _('Report a bug'),
            _('CaramOS or one of its apps does something wrong'), ['caramos-help', '--type', 'bug']);
        this._addAction(this._themed('chat-symbolic'), _('Send feedback'),
            _('An idea or a wish for CaramOS'), ['caramos-help', '--type', 'feedback']);
        this._addAction(this._themed('computer-symbolic'), _('Driver Check'),
            _('See which drivers this computer is missing'), ['caramos-driver-check']);
        this._requests = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._requests);

        // The list is ready before the menu opens: items added to an open menu get squeezed. The file
        // is watched, so a request sent a minute ago is listed; while the menu is open it waits.
        this._requestsStale = false;
        this._reloadId = 0;
        this._monitor = Gio.File.new_for_path(requestsFile()).monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._monitorId = this._monitor.connect('changed', () => this._requestsChanged());
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open && this._requestsStale) this._requestsChanged();
        });
        this._hasRequests = false;
        this._loadRequests();

        // caramos-help --refresh asks VNLF about the unfinished requests, writes what it says into the file
        // watched above and notifies a new answer itself.
        this._refreshing = false;
        this._refreshId = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, REFRESH_FIRST_SECONDS, () => {
            this._refresh();
            this._refreshId = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, REFRESH_EVERY_SECONDS, () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    on_applet_clicked() {
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        this._removed = true;
        if (this._reloadId) GLib.source_remove(this._reloadId);
        GLib.source_remove(this._refreshId);
        this._monitor.disconnect(this._monitorId);
        this._monitor.cancel();
        this.menu.destroy();
    }

    _refresh() {
        if (this._refreshing || !this._hasRequests) return;
        try {
            const process = Gio.Subprocess.new(['caramos-help', '--refresh'], Gio.SubprocessFlags.NONE);
            this._refreshing = true;
            process.wait_async(null, (source, result) => {
                try {
                    source.wait_finish(result);
                } catch (e) {
                    // Gone with the session.
                }
                this._refreshing = false;
            });
        } catch (e) {
            global.logError(e);
        }
    }

    // A write is several file events: read once they are over.
    _requestsChanged() {
        if (this.menu.isOpen) {
            this._requestsStale = true;
            return;
        }
        this._requestsStale = false;
        if (this._reloadId) GLib.source_remove(this._reloadId);
        this._reloadId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
            this._reloadId = 0;
            this._loadRequests();
            return GLib.SOURCE_REMOVE;
        });
    }

    // A title, not a menu item: Cinnamon draws menu items that cannot be clicked like disabled ones.
    _header() {
        const header = new St.BoxLayout({ style_class: 'popup-menu-item' });
        const [row] = twoLines(this._themed(ICON), _('Feedback & Help'),
            _('Write to the Vietnam Linux Family community'));
        header.add_child(row);
        return header;
    }

    _iconPath(name) {
        return GLib.build_filenamev([this._path, 'icons', name]);
    }

    _symbolic(name) {
        return new St.Icon({
            gicon: new Gio.FileIcon({ file: Gio.File.new_for_path(this._iconPath(name)) }),
            icon_type: St.IconType.SYMBOLIC,
        });
    }

    _themed(name) {
        return new St.Icon({ icon_name: name, icon_type: St.IconType.SYMBOLIC });
    }

    _addAction(icon, title, detail, argv) {
        const item = new TwoLineMenuItem(icon, title, detail);
        item.connect('activate', () => Util.spawn(argv));
        this.menu.addMenuItem(item);
    }

    _loadRequests() {
        const file = Gio.File.new_for_path(requestsFile());
        file.load_contents_async(null, (source, result) => {
            let requests = [];
            try {
                const [, contents] = source.load_contents_finish(result);
                requests = parseRequests(new TextDecoder('utf-8').decode(contents));
            } catch (e) {
                // Nothing sent yet: no list.
            }
            if (this._removed) return;
            this._hasRequests = requests.length > 0;
            if (this.menu.isOpen) this._requestsStale = true;
            else this._showRequests(requests);
        });
    }

    _showRequests(requests) {
        this._requests.removeAll();
        this._requests.box.destroy_all_children();   // the title is not a menu item: removeAll() leaves it
        if (!requests.length) return;
        this._requests.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const title = new St.BoxLayout({ style_class: 'popup-menu-item' });
        title.add_child(new St.Label({ text: _('Your requests'), style_class: 'caramos-help-section', opacity: 175 }));
        this._requests.addActor(title);
        const now = Date.now();
        const shown = requests.filter(request => listed(request, now)).slice(0, REQUESTS_SHOWN);
        for (const request of shown) {
            const name = [request.id, kindName(request.type), shortDate(request.sent)].filter(Boolean).join(' · ');
            const item = new TwoLineMenuItem(this._themed('web-browser-symbolic'), name, shorten(request.message),
                noteFor(request));
            if (finished(request)) item.row.opacity = 150;
            item.connect('activate', () => {
                try {
                    Gio.AppInfo.launch_default_for_uri(request.url, global.create_app_launch_context());
                } catch (e) {
                    global.logError(e);
                }
            });
            this._requests.addMenuItem(item);
        }
        // The others, and the ones finished long ago, are in the window.
        if (shown.length < requests.length) {
            const all = new TwoLineMenuItem(this._themed('view-list-symbolic'),
                _('See all %d requests').replace('%d', String(requests.length)));
            all.connect('activate', () => Util.spawn(['caramos-help', '--requests']));
            this._requests.addMenuItem(all);
        }
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new CaramOSHelpApplet(metadata, orientation, panelHeight, instanceId);
}
