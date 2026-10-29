const { Clutter, Gio, GLib, GObject, St } = imports.gi;
const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;
const ExtensionUtils = imports.misc.extensionUtils;

const Me = ExtensionUtils.getCurrentExtension();
const SCRIPT = GLib.build_filenamev([Me.path, 'attendo.py']);
const REFRESH_SECONDS = 30 * 60 * 60;

let indicator = null;

const AttendoIndicator = GObject.registerClass(
class AttendoIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Attendo');

        this._label = new St.Label({
            text: 'Attendo…',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._label);

        this._statusItem = new PopupMenu.PopupMenuItem('Loading Attendo…', {
            reactive: false,
        });
        this.menu.addMenuItem(this._statusItem);

        this._todayItem = this._addDetailItem("Today's goal");
        this._daysItem = this._addDetailItem('Days counted');
        this._paceItem = this._addDetailItem('Pace to finish');
        this._estimateItem = this._addDetailItem('Live estimate');

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const refreshItem = new PopupMenu.PopupMenuItem('Refresh now');
        refreshItem.connect('activate', () => this._refresh());
        this.menu.addMenuItem(refreshItem);
        this.menu.connect('open-state-changed', (_menu, isOpen) => {
            if (isOpen)
                this._refresh();
        });

        this._process = null;
        this._cancellable = new Gio.Cancellable();
        this._destroyed = false;
        this._refresh();
        this._timer = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            REFRESH_SECONDS,
            () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            }
        );
    }

    _addDetailItem(label) {
        const item = new PopupMenu.PopupMenuItem(`${label}: —`, {
            reactive: false,
        });
        this.menu.addMenuItem(item);
        return item;
    }

    _refresh() {
        // A slow network request must not accumulate more subprocesses every
        // time the timer fires or the user presses "Refresh now".
        if (this._process)
            return;

        try {
            const python = GLib.find_program_in_path('python3') || '/usr/bin/python3';
            this._process = Gio.Subprocess.new(
                [python, SCRIPT, '--panel'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            );
        } catch (error) {
            this._showError(error.message);
            return;
        }

        this._statusItem.label.set_text('Refreshing Attendo…');
        this._process.communicate_utf8_async(null, this._cancellable, (source, result) => {
            try {
                const [, stdout, stderr] = source.communicate_utf8_finish(result);
                if (this._destroyed)
                    return;
                if (source.get_successful()) {
                    const data = JSON.parse(stdout);
                    this._label.set_text(`⏳ ${data.time_left}`);
                    this._todayItem.label.set_text(`Today's goal: ${data.today_goal}`);
                    this._daysItem.label.set_text(`Days counted: ${data.days_counted}`);
                    this._paceItem.label.set_text(`Pace to finish: ${data.pace}`);
                    this._estimateItem.label.set_text(`Live estimate: ${data.live_estimate}`);
                    const updated = new Date().toLocaleTimeString([], {
                        hour: '2-digit',
                        minute: '2-digit',
                    });
                    this._statusItem.label.set_text(`Updated at ${updated}`);
                } else {
                    this._showError(stderr.trim() || 'Could not refresh Attendo');
                }
            } catch (error) {
                if (!this._destroyed)
                    this._showError(error.message);
            } finally {
                this._process = null;
            }
        });
    }

    _showError(message) {
        if (this._destroyed)
            return;
        this._label.set_text('Attendo !');
        // Keep the popup usable when Python returns a multi-line traceback or
        // an unexpectedly long network error.
        const detail = String(message).split('\n')[0].slice(0, 160);
        this._statusItem.label.set_text(detail || 'Could not refresh Attendo');
    }

    destroy() {
        this._destroyed = true;
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        this._cancellable.cancel();
        if (this._process)
            this._process.force_exit();
        super.destroy();
    }
});

function init() {}

function enable() {
    indicator = new AttendoIndicator();
    Main.panel.addToStatusArea('attendo-indicator', indicator, 0, 'right');
}

function disable() {
    if (indicator) {
        indicator.destroy();
        indicator = null;
    }
}
