import unittest
import asyncio
import tempfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dataclasses import replace
from radio_configurator import App, Channel, _TONE_DATA
from radio_configurator import parse_frequency
from n1_protocol import uint


class RadioUiTests(unittest.TestCase):
    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_patch = patch("radio_configurator.CONFIG_INI",Path(self.config_dir.name)/"config.ini")
        self.config_patch.start()
        self.app = App()
        self.app.withdraw()

    def tearDown(self):
        self.app.update_idletasks()
        for callback in self.app.tk.call("after", "info"):
            self.app.after_cancel(callback)
        self.app.ble.client = None
        self.app.close()
        self.config_patch.stop()
        self.config_dir.cleanup()

    def test_read_selection_restores_all_tone_modes_and_indexes(self):
        a = self.app
        for mode, key in [(1, "analog"), (2, "digital_normal"), (3, "digital_inverse")]:
            for index, label in enumerate(_TONE_DATA[key]):
                value = (mode << 8) + index
                c = Channel(rx_tone=value, tx_tone=value)
                a.channels = [c]; a.refresh(); a.tree.selection_set("2:1"); a.select()
                for selector in a.tones.values():
                    self.assertEqual(selector.mode.get(), mode)
                    self.assertEqual(selector.label.get(), label)
                    self.assertEqual(selector.get_value(), value)
                self.assertEqual(a.vars["kind"].get(), "2")
        a.tones["rxt"].set_value(0)
        self.assertEqual(a.tones["rxt"].get_value(), 0)
        self.assertEqual(str(a.tones["rxt"].box["state"]), "disabled")

    def test_disconnect_preserves_rows_snapshot_and_unsaved_editor(self):
        a = self.app
        c = Channel(rx_tone=260, tx_tone=515)
        a.channels = [c]; a.original = [replace(c)]; a.snapshot_complete = True
        a.refresh(); a.tree.selection_set("2:1"); a.select()
        a.vars["name"].set("unsaved edit")
        a.setting_values[7].set("saved name")
        a._disconnected()
        self.assertEqual(a.channels, [c])
        self.assertEqual(a.original, [c])
        self.assertEqual(a.vars["name"].get(), "unsaved edit")
        self.assertEqual(a.setting_values[7].get(), "saved name")
        self.assertEqual(a.tones["rxt"].get_value(), 260)
        self.assertEqual(len(a.tree.get_children()), 1)
        self.assertNotIn("op", a.tree["columns"])

    def test_column_weights_are_ratios(self):
        a = self.app
        a.column_weights = dict(seq=1, kind=2, name=1, rx=1, tx=1, rxt=3, txt=3)
        a.resize_columns(SimpleNamespace(width=1204))
        self.assertEqual(a.tree.column("kind", "width"), 2*a.tree.column("seq", "width"))
        self.assertEqual(a.tree.column("rxt", "width"), 3*a.tree.column("seq", "width"))

    def test_preset_fixed_fields_and_operations_are_disabled(self):
        a = self.app
        preset = Channel(kind=1, name="", rx_tone=260)
        custom = Channel(kind=2)
        a.channels = [preset, custom]; a.original = [replace(preset), replace(custom)]
        a.snapshot_complete = True
        a.ble.client = SimpleNamespace(is_connected=True)
        a.refresh(); a.tree.selection_set("1:1"); a.select()
        for key in ("seq", "name", "rx", "tx"):
            self.assertEqual(str(a.editor_widgets[key]["state"]), "disabled")
        self.assertEqual(str(a.add_btn["state"]), "disabled")
        self.assertEqual(str(a.delete_btn["state"]), "disabled")
        self.assertEqual(str(a.tones["rxt"].box["state"]), "readonly")
        self.assertTrue(all(str(button["state"]) == "disabled" for button in a.kind_buttons))
        a.tree.selection_set("2:1"); a.select()
        for key in ("name", "rx", "tx"):
            self.assertEqual(str(a.editor_widgets[key]["state"]), "normal")
        self.assertEqual(str(a.add_btn["state"]), "normal")
        self.assertEqual(str(a.delete_btn["state"]), "normal")

    def test_reorder_tracks_config_selection_and_delays_actual_switch(self):
        a = self.app
        four = Channel(seq=4, name="four", raw="3801")
        five = Channel(seq=5, name="five", raw="3800")
        a.channels = [four, five]; a.original = [replace(four), replace(five)]
        a.snapshot_complete = True
        a.device_current_channel = a.current_channel = four.key
        a.refresh(); a.tree.selection_set("2:5"); a.select()
        a.move_channel(-1)
        self.assertEqual(a.selected().key, (2,4))
        self.assertEqual(a.selected().name, "five")
        self.assertEqual(a.selected().raw, five.raw)
        self.assertEqual(a.current_channel, (2,5))
        self.assertEqual(a.device_current_channel, (2,4))
        self.assertEqual(a.order_mapping, {(2,4):(2,5), (2,5):(2,4)})
        self.assertEqual(a.tree.item("2:5", "tags"), ("current",))
        self.assertEqual(a.original, [four,five])
        a.move_channel(1)
        self.assertEqual(a.selected().key, (2,5))
        self.assertEqual(a.current_channel, (2,4))
        self.assertEqual(a.order_mapping, {})

    def test_direct_insert_and_delete_keep_other_local_edits(self):
        a = self.app
        first = Channel(seq=1,name="original")
        a.original = [first]; a.channels = [replace(first,name="unsaved")]
        a.snapshot_complete = True; a.ble.client = SimpleNamespace(is_connected=True)
        calls = []
        async def save(channel,existing):
            calls.append(("insert",channel.key,existing)); return channel
        async def delete(channel):
            calls.append(("delete",channel.key)); return [first]
        a.ble.session = SimpleNamespace(save_channel=save,delete_channel=delete,current=first.key)
        a.run = lambda coro,done: done(asyncio.run(coro))
        a.insert_channel(Channel(seq=2,name="new"))
        self.assertEqual(a.channels[0].name,"unsaved")
        self.assertEqual(a.original[0].name,"original")
        with patch("radio_configurator.messagebox.askyesno",return_value=False):
            a.delete()
        self.assertEqual(len(calls),1)
        with patch("radio_configurator.messagebox.askyesno",return_value=True):
            a.delete()
        self.assertEqual(calls,[("insert",(2,2),False),("delete",(2,2))])
        self.assertEqual([c.name for c in a.channels],["unsaved"])
        self.assertEqual(a.original,[first])

    def test_double_click_targets_clicked_row(self):
        a = self.app
        a.channels = [Channel(seq=4),Channel(seq=5)]; a.refresh()
        with patch.object(a.tree,"identify_row",return_value="2:5"), patch.object(a,"switch_selected") as switch:
            a.double_click_channel(SimpleNamespace(y=20))
        self.assertEqual(a.selected().key,(2,5))
        switch.assert_called_once()

    def test_grouped_frequency_roundtrip_and_preset_disable(self):
        a = self.app
        c = Channel(rx=430137500,tx=431002005)
        a.channels=[c]; a.original=[replace(c)]
        a.refresh(); a.tree.selection_set("2:1"); a.select()
        self.assertEqual(a.tree.item("2:1","values")[3:5],("430 137 500","431 002 005"))
        rx = a.editor_widgets["rx"]
        self.assertEqual([v.get() for v in rx.parts],["430","137","500"])
        rx.parts[1].set("002"); rx.parts[2].set("005")
        self.assertEqual(a.apply().rx,430002005)
        rx.parts[1].set("")
        with patch("radio_configurator.messagebox.showerror") as error:
            self.assertIsNone(a.apply())
        error.assert_called_once()
        a.channels=[replace(c,kind=1)]; a.refresh(); a.tree.selection_set("1:1"); a.select()
        self.assertTrue(all(str(w["state"])=="disabled" for w in rx.entries))

    def test_device_radio_choices_roundtrip_and_sparse_write(self):
        a = self.app
        info={7:b"N1-test",10:1,12:0,13:2,15:0,9: uint(7,0)+uint(8,1)+uint(12,0)+uint(13,1)}
        a.show_info(info)
        self.assertEqual(a.setting_values[13].get(),"中文")
        for tag,widget in a.setting_widgets.items():
            if tag != 7:
                self.assertTrue(all(w.winfo_class()=="TRadiobutton" and str(w["state"])=="normal" for w in widget.buttons))
        a.setting_widgets[13].buttons[2].invoke()
        a.setting_widgets["ch12"].buttons[1].invoke()
        a.ble.client=SimpleNamespace(is_connected=True)
        with patch.object(a,"_session_action") as action:
            a.write_info()
        self.assertEqual(action.call_args.args,("update_device_info",{13:3},{12:1}))
        a.show_info({10:1})
        self.assertTrue(all(str(w["state"])=="disabled" for w in a.setting_widgets[13].buttons))

    def test_frequency_overwrite_and_advance_without_insertion(self):
        a=self.app
        a.channels=[Channel(rx=430137500,tx=431002005)]
        a.refresh(); a.tree.selection_set("2:1"); a.select()
        rx=a.editor_widgets["rx"]; tx=a.editor_widgets["tx"]
        rx.entries[0].icursor(0)
        with patch.object(rx.entries[1],"focus_set") as focus:
            for char in "145":
                self.assertEqual(rx._overwrite(SimpleNamespace(char=char,state=0),0),"break")
        focus.assert_called_once()
        self.assertEqual(rx.parts[0].get(),"145")
        self.assertEqual(rx.entries[1].index("insert"),0)
        self.assertEqual(a.vars["rx"].get(),"145137500")
        rx.entries[2].icursor(2)
        with patch.object(tx.entries[0],"focus_set") as focus:
            rx._overwrite(SimpleNamespace(char="8",state=0),2)
        focus.assert_called_once()
        self.assertEqual(tx.entries[0].index("insert"),0)
        self.assertEqual(a.vars["rx"].get(),"145137508")

    def test_frequency_copy_buttons_and_preset_protection(self):
        a=self.app
        c=Channel(rx=430137500,tx=431002005)
        a.channels=[c]; a.refresh(); a.tree.selection_set("2:1"); a.select()
        rx=a.editor_widgets["rx"]; tx=a.editor_widgets["tx"]
        tx.copy_button.invoke()
        self.assertEqual(a.vars["tx"].get(),"430137500")
        a.vars["tx"].set("435123456"); rx.copy_button.invoke()
        self.assertEqual([v.get() for v in rx.parts],["435","123","456"])
        a.channels=[replace(c,kind=1)]; a.refresh(); a.tree.selection_set("1:1"); a.select()
        self.assertEqual(str(rx.copy_button["state"]),"disabled")
        rx.copy_button.invoke()
        self.assertEqual(a.vars["rx"].get(),str(c.rx))

    def test_windows_numlock_keyboard_events_overwrite_and_move_focus(self):
        a = self.app
        if a.tk.call("tk", "windowingsystem") != "win32":
            self.skipTest("Windows Num Lock event mask")
        a.deiconify(); a.update()
        for state in (0,8,10):
            a.channels=[Channel(rx=430137500,tx=431002005)]
            a.refresh(); a.tree.selection_set("2:1"); a.select(); a.update()
            rx=a.editor_widgets["rx"]; tx=a.editor_widgets["tx"]
            rx.entries[0].focus_force(); rx.entries[0].icursor(0); a.update()
            for i,char in enumerate("145234678"):
                a.focus_get().event_generate("<KeyPress>",keysym=char,state=state)
                a.update()
                if i in (2,5):
                    self.assertIs(a.focus_get(),rx.entries[(i+1)//3])
                    self.assertEqual(a.focus_get().index("insert"),0)
            self.assertEqual(a.vars["rx"].get(),"145234678")
            self.assertEqual([v.get() for v in rx.parts],["145","234","678"])
            self.assertIs(a.focus_get(),tx.entries[0])
            self.assertEqual(tx.entries[0].index("insert"),0)
        event=SimpleNamespace(char="5",state=4)
        self.assertIsNone(rx._overwrite(event,0))
        event.state=0x20000
        self.assertIsNone(rx._overwrite(event,0))

    def test_dirty_editor_and_single_save_only_clear_target(self):
        a=self.app
        first=Channel(seq=1); second=Channel(seq=2)
        a.channels=[first,replace(second,name="pending")]; a.original=[replace(first),replace(second)]
        a.snapshot_complete=True; a.loaded_kinds={2}; a.ble.client=SimpleNamespace(is_connected=True)
        a.refresh(); a.tree.selection_set("2:1"); a.select()
        self.assertEqual(str(a.save_one_btn["state"]),"disabled")
        a.vars["name"].set("changed")
        self.assertEqual(a.pending_text.get(),"待保存 2 个频道")
        self.assertTrue(a.tree.set("2:1","name").endswith(" *"))
        async def save(channel,existing): return channel
        a.ble.session=SimpleNamespace(save_channel=save)
        a.run=lambda coro,done: done(asyncio.run(coro))
        a.save_one()
        self.assertEqual(a.pending_text.get(),"待保存 1 个频道")
        self.assertFalse(a.tree.set("2:1","name").endswith(" *"))
        self.assertTrue(a.tree.set("2:2","name").endswith(" *"))
        self.assertEqual(str(a.save_one_btn["state"]),"disabled")

    def test_refresh_keeps_editor_selection_and_scroll(self):
        a=self.app; a.deiconify(); a.update()
        a.channels=[Channel(seq=i,name=str(i)) for i in range(1,80)]
        a.original=[replace(c) for c in a.channels]; a.snapshot_complete=True
        a.refresh(); a.tree.selection_set("2:40"); a.select(); a.tree.yview_moveto(0.5); a.update()
        a.vars["name"].set("typing"); view=a.tree.yview()
        with patch.object(a.tree,"delete",wraps=a.tree.delete) as deleted:
            a._state((2,20)); a.update()
        deleted.assert_not_called()
        self.assertEqual(a.tree.selection(),("2:40",))
        self.assertEqual(a.vars["name"].get(),"typing")
        self.assertAlmostEqual(a.tree.yview()[0],view[0],places=3)

    def test_frequency_paste_and_incomplete_hint(self):
        for text in ["430137500","430 137 500","430.137500 MHz"]:
            self.assertEqual(parse_frequency(text),430137500)
        for text in ["430.137500","abc","-1","0","430.1375001 MHz","4294967296"]:
            self.assertIsNone(parse_frequency(text))
        a=self.app; a.channels=[Channel()]; a.refresh(); a.tree.selection_set("2:1"); a.select()
        rx=a.editor_widgets["rx"]
        with patch.object(rx,"clipboard_get",return_value="430.137500 MHz"):
            rx.paste_button.invoke()
        self.assertEqual(a.vars["rx"].get(),"430137500")
        with patch.object(rx,"clipboard_get",return_value="invalid"):
            rx.paste_button.invoke()
        self.assertEqual(a.vars["rx"].get(),"430137500")
        rx.parts[1].set("13")
        self.assertEqual(a.vars["rx"].get(),"")
        self.assertEqual(rx.hint["text"],"频率未填写完整")
        rx.entries[1].icursor(0)
        with patch.object(rx.entries[0],"focus_set") as focus:
            rx._overwrite(SimpleNamespace(char="",state=8,keysym="BackSpace"),1)
        focus.assert_called_once()

    def test_filtered_read_and_save_do_not_touch_other_bank(self):
        a=self.app
        preset=Channel(kind=1,name=""); custom=Channel(seq=1)
        a.channels=[preset,replace(custom,name="custom edit")]; a.original=[replace(preset),replace(custom)]
        a.loaded_kinds={1,2}; a.snapshot_complete=True; a.ble.client=SimpleNamespace(is_connected=True)
        a.channel_filter.set("预设")
        async def read(progress,kinds):
            self.assertEqual(kinds,(1,)); return [preset]
        a.ble.read_channels=read; a.ble.session=SimpleNamespace(current=(1,1))
        a.run=lambda coro,done: done(asyncio.run(coro)); a.read_info=lambda:None
        a.read_device()
        self.assertEqual(next(c for c in a.channels if c.kind==2).name,"custom edit")
        a.tree.selection_set("1:1"); a.select(); a.tones["rxt"].set_value(260)
        async def sync(desired,original,mapping,kinds):
            self.assertEqual(kinds,(1,))
            self.assertTrue(all(c.kind==1 for c in desired+original))
            return desired
        a.ble.sync=sync; a.write_device()
        self.assertEqual(next(c for c in a.channels if c.kind==2).name,"custom edit")
        self.assertEqual(a.pending_text.get(),"待保存 1 个频道")

    def test_config_remembers_filter_window_split_and_columns(self):
        a=self.app; a.deiconify(); a.update()
        a.channel_filter.set("自定义")
        a.geometry("1200x740+30+40"); a.update()
        a.pane.sashpos(0,750)
        a.tree.column("name",width=180); a.remember_columns()
        a.save_config(layout=True)
        from radio_configurator import CONFIG_INI
        import configparser
        cp=configparser.ConfigParser(); cp.read(CONFIG_INI,encoding="utf8")
        self.assertEqual(cp.get("ui","channel_filter"),"自定义")
        self.assertEqual(cp.get("window","geometry"),"1200x740+30+40")
        self.assertGreater(cp.getfloat("column_widths","name"),150)
        self.assertAlmostEqual(cp.getfloat("window","split"),750/a.pane.winfo_width())

    def test_operation_status_pending_success_and_failure_colors(self):
        a=self.app
        def call(coro):
            coro.close(); future=Future(); future.set_result(None); return future
        a.ble.call=call
        a.set_status("保存中...")
        a.run(asyncio.sleep(0),lambda _:a.set_status("保存成功"))
        self.assertEqual(str(a.status_label["foreground"]),"black")
        a._drain()
        self.assertEqual(str(a.status_label["foreground"]),"#16803c")
        def fail(coro):
            coro.close(); future=Future(); future.set_exception(RuntimeError("test failure")); return future
        a.ble.call=fail; a.set_status("读取中...")
        a.run(asyncio.sleep(0),lambda _:None)
        with patch("radio_configurator.messagebox.showerror"):
            a._drain()
        self.assertEqual(str(a.status_label["foreground"]),"#c62828")
        self.assertIn("test failure",a.status.get())


if __name__ == "__main__":
    unittest.main()
