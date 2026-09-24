"""
Симулятор модема Huawei для тестового режима (без роутера).

Отвечает на те же вызовы, что huawei_lte_api.Client, поэтому тестовый
режим проходит через RouterSession/SessionWorker точно так же, как
реальный роутер. Симулятор:
  * «поворачивает антенну» (3°/с) — уровень каждой соты зависит от угла;
  * выбирает лучшую разрешённую Band Lock соту (смена соты, CA, 5G NSA);
  * реагирует на Band Lock (перерегистрация), смену антенны,
    перезагрузку (роутер пропадает, старая сессия истекает) и
    выключение мобильных данных.
"""
from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.constants import LTE_ALL_MASK

DEMO_SUPPORTED_BANDS: tuple[int, ...] = (1, 3, 7, 8, 20, 38)
REBOOT_SECONDS = 12.0
REREGISTER_SECONDS = 3.0


class DemoApiError(Exception):
    """Ошибка API симулятора (как ResponseErrorException: есть .code)."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class DemoCell:
    pci: int
    earfcn: int
    band: int
    enodeb: int
    sector: int
    azimuth: float          # направление на соту, градусы
    peak_rsrp: float
    peak_sinr: float
    ca_band: int | None = None
    nr: bool = False


DEMO_CELLS: tuple[DemoCell, ...] = (
    DemoCell(287, 1300, 3, 180123, 12, 40.0, -78.0, 19.0, ca_band=1, nr=True),
    DemoCell(112, 6300, 20, 180456, 1, 210.0, -84.0, 13.0),
    DemoCell(45, 3000, 7, 181002, 23, 125.0, -92.0, 8.0),
)
_UL_OFFSET = {1: 18000, 3: 18000, 7: 18000, 8: 18000, 20: 18000, 38: 0}


def _angle_gain(angle: float, azimuth: float, cap: float) -> float:
    diff = abs(angle - azimuth) % 360.0
    diff = min(diff, 360.0 - diff)
    return max(cap, -14.0 * (diff / 60.0) ** 2)


class DemoModem:
    """Состояние симулируемого модема (общее для всех «сессий»)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 seed: int = 7) -> None:
        self._clock = clock
        self._t0 = clock()
        self._rng = random.Random(seed)
        self.lte_mask = LTE_ALL_MASK
        self.network_band = '3FFFFFFF'
        self.network_mode = '00'
        self.antenna = 3
        self.data_on = True
        self.boot_id = 0
        self.offline_until = 0.0
        self.reregister_until = 0.0
        self.total_dl = 0
        self.total_ul = 0

    # ---- модель радиоканала ----

    def now(self) -> float:
        return self._clock() - self._t0

    def angle(self) -> float:
        return (self.now() * 3.0) % 360.0

    def check_online(self) -> None:
        if self.now() < self.offline_until:
            raise ConnectionError("Demo router is rebooting")

    def _allowed(self, band: int) -> bool:
        return bool(self.lte_mask & (1 << (band - 1)))

    def _levels(self, cell: DemoCell) -> tuple[float, float]:
        angle = self.angle()
        ext = self.antenna != 0          # внутренняя антенна — хуже на 12 дБ
        rsrp = cell.peak_rsrp + _angle_gain(angle, cell.azimuth, -30.0)
        sinr = cell.peak_sinr + _angle_gain(angle, cell.azimuth, -22.0) * 0.8
        if not ext:
            rsrp -= 12.0
            sinr -= 6.0
        return rsrp, sinr

    def serving(self) -> DemoCell | None:
        if self.now() < self.reregister_until:
            return None
        best: DemoCell | None = None
        best_rsrp = -999.0
        for cell in DEMO_CELLS:
            if not self._allowed(cell.band):
                continue
            rsrp, _ = self._levels(cell)
            if rsrp > best_rsrp:
                best, best_rsrp = cell, rsrp
        return best

    def ca_active(self, cell: DemoCell) -> bool:
        return (cell.ca_band is not None and self._allowed(cell.ca_band)
                and abs(self._levels(cell)[0] - cell.peak_rsrp) < 10.0)

    def nr_active(self, cell: DemoCell) -> bool:
        return cell.nr and self._levels(cell)[1] >= 6.0 and self.network_mode != '03'

    def signal(self) -> dict[str, Any]:
        cell = self.serving()
        if cell is None:
            return {'rsrp': '', 'sinr': '', 'rsrq': '', 'rssi': '', 'pci': '',
                    'cell_id': '', 'earfcn': '', 'band': ''}
        rsrp, sinr = self._levels(cell)
        rsrp += self._rng.gauss(0, 0.8)
        sinr += self._rng.gauss(0, 1.0)
        rsrq = max(-19.5, min(-3.0, -10.0 + (sinr - 10.0) * 0.35))
        rssi = rsrp + 25.0 + self._rng.uniform(-1, 1)
        ca = self.ca_active(cell)
        cqi0 = int(max(1, min(15, round(sinr / 2 + 5))))
        cqi1 = max(0, cqi0 - 1) if self.antenna in (1, 3) else 0
        pusch = max(-10.0, min(23.0, 10.0 + (-80.0 - rsrp) * 0.6))
        data: dict[str, Any] = {
            'rsrp': f"{round(rsrp)}dBm",
            'rsrq': f"{rsrq:.1f}dB",
            'rssi': f"{round(rssi)}dBm",
            'sinr': f"{round(sinr)}dB",
            'pci': str(cell.pci),
            'cell_id': str(cell.enodeb * 256 + cell.sector),
            'earfcn': f"DL:{cell.earfcn} UL:{cell.earfcn + _UL_OFFSET.get(cell.band, 18000)}",
            'band': f"B{cell.band}+B{cell.ca_band}" if ca else str(cell.band),
            'dlbandwidth': '20MHz',
            'ulbandwidth': '20MHz',
            'tac': '12345',
            'transmode': 'TM[3]',
            'cqi0': str(cqi0),
            'cqi1': str(cqi1),
            'dl_mcs': f"mcsDownCarrier1Code0:{min(27, cqi0 * 2)}@256QAM "
                      f"mcsDownCarrier1Code1:{min(27, cqi1 * 2)}@256QAM",
            'ul_mcs': f"mcsUpCarrier1:{min(28, cqi0 + 8)}@64QAM",
            'txpower': f"PPusch:{pusch:.0f}dBm PPucch:6dBm",
            'rrc_status': '1',
        }
        if self.nr_active(cell):
            data.update({
                'nrrsrp': f"{round(rsrp - 7)}dBm",
                'nrsinr': f"{round(sinr - 3)}dB",
                'nrrsrq': '-11dB',
                'nrearfcn': '627264',
                'nrpci': '501',
                'nrband': '78',
                'nrdlbandwidth': '100MHz',
            })
        return data

    def status(self) -> dict[str, Any]:
        cell = self.serving()
        ex = '0' if cell is None else ('1011' if self.ca_active(cell) else '101')
        return {
            'CurrentNetworkTypeEx': ex,
            'CurrentNetworkType': '19' if cell else '0',
            'ConnectionStatus': '901' if self.data_on and cell else '902',
            'SimStatus': '1',
            'SignalIcon': '4',
        }

    def traffic(self) -> dict[str, Any]:
        dl = self._rng.randint(1_000_000, 9_000_000) if self.data_on else 0
        ul = self._rng.randint(100_000, 2_000_000) if self.data_on else 0
        self.total_dl += dl
        self.total_ul += ul
        return {
            'CurrentDownloadRate': str(dl),
            'CurrentUploadRate': str(ul),
            'TotalDownload': str(self.total_dl),
            'TotalUpload': str(self.total_ul),
            'CurrentConnectTime': str(int(self.now())),
        }

    # ---- действия ----

    def set_net_mode(self, lteband: Any, networkband: Any, networkmode: Any) -> str:
        self.lte_mask = int(str(lteband), 16)
        self.network_band = str(networkband)
        self.network_mode = str(getattr(networkmode, 'value', networkmode))
        self.reregister_until = self.now() + REREGISTER_SECONDS
        return 'OK'

    def reboot(self) -> None:
        self.boot_id += 1
        self.offline_until = self.now() + REBOOT_SECONDS
        self.data_on = True


class _Group:
    """Группа API (device/net/...) с проверкой сессии перед каждым вызовом."""

    def __init__(self, client: DemoClient) -> None:
        self._client = client

    def _check(self) -> DemoModem:
        modem = self._client.modem
        modem.check_online()
        if self._client.boot_id != modem.boot_id or self._client.closed:
            raise DemoApiError("No rights (needs login)", 100003)
        return modem


class _Device(_Group):
    def information(self) -> dict[str, Any]:
        self._check()
        return {
            'DeviceName': 'B636-336 (demo)', 'SerialNumber': 'DEMO1234567890',
            'Imei': '860000000000001', 'Imsi': '250020000000001',
            'Iccid': '8970102000000000001', 'Msisdn': '+79000000000',
            'SoftwareVersion': '11.0.1.1(DEMO)', 'HardwareVersion': 'WL1B636M',
            'ProductFamily': 'LTE', 'Classify': 'cpe', 'workmode': 'LTE',
        }

    def signal(self) -> dict[str, Any]:
        return self._check().signal()

    def get_antenna_settings(self) -> dict[str, Any]:
        return {'antenna_type': str(self._check().antenna)}

    def antenna_type(self) -> dict[str, Any]:
        return self.get_antenna_settings()

    def antenna_status(self) -> dict[str, Any]:
        return self.get_antenna_settings()

    def antenna_set_type(self) -> dict[str, Any]:
        return self.get_antenna_settings()

    def set_antenna_settings(self, antenna_type: Any) -> str:
        self._check().antenna = int(antenna_type)
        return 'OK'

    def set_control(self, control: Any) -> str:
        modem = self._check()
        if int(control) == 1:
            modem.reboot()
            return 'OK'
        raise DemoApiError("No support", 100002)


class _Net(_Group):
    def current_plmn(self) -> dict[str, Any]:
        self._check()
        return {'State': '0', 'FullName': 'MegaFon', 'ShortName': 'MegaFon',
                'Numeric': '25002', 'Rat': '7'}

    def net_mode(self) -> dict[str, Any]:
        modem = self._check()
        return {'NetworkMode': modem.network_mode,
                'NetworkBand': modem.network_band,
                'LTEBand': format(modem.lte_mask, 'X')}

    def net_mode_list(self) -> dict[str, Any]:
        self._check()
        bands = [{'Name': f"LTE BC{b}", 'Value': format(1 << (b - 1), 'X')}
                 for b in DEMO_SUPPORTED_BANDS]
        bands.append({'Name': 'LTE ALL', 'Value': '7FFFFFFFFFFFFFFF'})
        return {'AccessList': {'Access': ['00', '01', '02', '03']},
                'LTEBandList': {'LTEBand': bands}}

    def set_net_mode(self, lteband: Any, networkband: Any, networkmode: Any) -> str:
        return self._check().set_net_mode(lteband, networkband, networkmode)

    def cell_info(self) -> dict[str, Any]:
        self._check()
        raise DemoApiError("No support", 100002)


class _Monitoring(_Group):
    def status(self) -> dict[str, Any]:
        return self._check().status()

    def traffic_statistics(self) -> dict[str, Any]:
        return self._check().traffic()

    def month_statistics(self) -> dict[str, Any]:
        modem = self._check()
        return {'CurrentMonthDownload': str(8 * 1024 ** 3 + modem.total_dl),
                'CurrentMonthUpload': str(1024 ** 3 + modem.total_ul)}


class _DialUp(_Group):
    def mobile_dataswitch(self) -> dict[str, Any]:
        return {'dataswitch': '1' if self._check().data_on else '0'}

    def set_mobile_dataswitch(self, dataswitch: int = 0) -> str:
        self._check().data_on = bool(int(dataswitch))
        return 'OK'


class DemoClient:
    """Клиент симулятора с той же структурой, что huawei_lte_api.Client."""

    def __init__(self, modem: DemoModem) -> None:
        self.modem = modem
        self.boot_id = modem.boot_id
        self.closed = False
        self.device = _Device(self)
        self.net = _Net(self)
        self.monitoring = _Monitoring(self)
        self.dial_up = _DialUp(self)


class DemoConnection:
    def __init__(self, client: DemoClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.closed = True


def demo_factory(modem: DemoModem | None = None) -> Callable[..., tuple[Any, Any]]:
    """Фабрика клиентов для RouterSession(factory=...)."""
    shared = modem or DemoModem()

    def factory(url: str, username: str, password: str,
                timeout: float) -> tuple[Any, Any]:
        shared.check_online()
        client = DemoClient(shared)
        return DemoConnection(client), client

    return factory


def demo_angle_hint(modem: DemoModem) -> str:
    """Текущий «азимут» симулятора — для подписи в тестовом режиме."""
    return f"{math.floor(modem.angle()):d}°"
