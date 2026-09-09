"""新增自动发现的定点验证；虚拟工具路径，不连接模拟器或平台。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from opinion_capture import android_discovery as discovery
from opinion_capture.android import AdbClient


class AndroidDiscoveryTests(unittest.TestCase):
    def test_registry_fallback_when_process_paths_are_not_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "任意盘符安装" / "nx_main"
            tools.mkdir(parents=True)
            (tools/"adb.exe").touch()
            with patch.object(discovery,"_cache_at",0), patch.object(discovery,"_cache",[]), \
                 patch.object(discovery,"_process_paths",return_value=[]), \
                 patch.object(discovery,"_registry_paths",return_value=[str(tools/"MuMuPlayer.exe")]) as registry:
                rows=discovery.running_mumu_tools(time.monotonic()+3)
                self.assertEqual(rows[0]["adb"],str(tools/"adb.exe"))
                registry.assert_called_once()

    def test_selected_path_survives_restart_without_global_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            adb=Path(directory)/"adb.exe"
            adb.touch()
            record=Path(directory)/".local/android-tools.json"
            with patch.object(discovery,"_selection_file",return_value=record), \
                 patch.object(discovery,"running_mumu_tools") as scan:
                discovery.remember_adb(str(adb))
                self.assertEqual(discovery.find_adb(time.monotonic()+1,environ={}),str(adb))
                self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["adb"],str(adb))
                scan.assert_not_called()

    def test_running_process_resolves_arbitrary_install_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "任意安装目录" / "nx_main"
            home.mkdir(parents=True)
            for name in ("adb.exe", "MuMuManager.exe", "MuMuNxService.exe"):
                (home / name).touch()
            with patch.object(discovery, "_cache_at", 0), \
                 patch.object(discovery, "_cache", []), \
                 patch.object(discovery, "_process_paths", return_value=[str(home/"MuMuNxService.exe")]) as scan, \
                 patch.object(discovery.shutil, "which", return_value=None):
                first = discovery.find_adb(time.monotonic()+3, environ={})
                second = discovery.find_adb(time.monotonic()+3, environ={})
                self.assertEqual(first, str(home/"adb.exe"))
                self.assertEqual(first, second)
                scan.assert_called_once()

    def test_existing_path_skips_process_discovery(self):
        with patch.object(discovery.shutil, "which", return_value="any/adb.exe"), \
             patch.object(discovery, "running_mumu_tools") as scan:
            self.assertEqual(discovery.find_adb(time.monotonic()+3,environ={}), "any/adb.exe")
            scan.assert_not_called()

    def test_manager_only_returns_running_local_ports(self):
        rows = {"0":{"is_android_started":True,"adb_port":17531,"name":"设备一"},
                "1":{"is_android_started":False,"adb_port":17532},
                "2":{"is_android_started":True,"adb_port":"17533","name":"设备三"},
                "3":{"is_android_started":True,"adb_port":65536}}
        with patch.object(discovery,"_tools_near",return_value=[{"adb":"adb","manager":"manager"}]):
            commands=[]
            def run(command):
                commands.append(command)
                return subprocess.CompletedProcess(command,0,json.dumps(rows).encode(),b"")
            found=discovery.running_instances("adb",time.monotonic()+3,run)
            self.assertEqual([r["serial"] for r in found],["127.0.0.1:17531","127.0.0.1:17533"])
            self.assertEqual(commands,[["manager","info","--vmindex","all"]])

    def test_refresh_connects_only_missing_instance_and_selected_device_reuses(self):
        client=AdbClient(time.monotonic()+10,executable="adb")
        before="List of devices attached\n127.0.0.1:17531 device model:MuMu\n"
        after=before+"127.0.0.1:17533 device model:MuMu\n"
        instances=[{"serial":"127.0.0.1:17531","name":"一"},{"serial":"127.0.0.1:17533","name":"三"}]
        with patch.object(client,"text",side_effect=[before,"connected",after]) as adb, \
             patch.object(discovery,"running_instances",return_value=instances):
            rows=client.list_devices(refresh=True)
            self.assertEqual([r["name"] for r in rows],["一","三"])
            self.assertEqual([c.args for c in adb.call_args_list],[("devices","-l"),("connect","127.0.0.1:17533"),("devices","-l")])
        with patch.object(client,"text",return_value=after) as adb, \
             patch.object(discovery,"running_instances") as manager:
            self.assertEqual(len(client.list_devices("127.0.0.1:17533")),2)
            adb.assert_called_once_with("devices","-l",device=False)
            manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
