# -*- coding: utf-8 -*-
"""
高低模批量命名工具 v5.7 (工具箱模块版)

v5.7 新增 / 修复:
- 集成"孤立 intermediate shape"静默清理:预览/执行前自动清理,
  带蒙皮/BS/变形器的资产严格跳过,不影响有效数据。
- 清理动作纳入 undo chunk,与后续重命名合并成一次撤销。
- _bbox_center / _face_count 加异常保护,遇到无效节点时跳过而非崩溃。
- _safe_rename 对 cmds.ls 返回值做空值保护,避免极端场景索引越界。
- _unique_group 返回 long path,避免同名冲突时 parent 误指向别的节点。

v5.6:
- _safe_rename 用 long path 判断,避免短名冲突误跳过
- reorder_group_children_by_name 只处理 transform 子节点
- Renamer.run 在 undo chunk 内积攒日志,循环结束后一次性输出
- 清理冗余代码

v5.5:
- 每次打开 UI 时所有输入项恢复默认值

v5.4:
- 关闭后重开日志清空 + 选区刷新
"""

import re
from collections import defaultdict

import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide2 import QtCore, QtWidgets, QtGui
    from shiboken2 import wrapInstance
except ImportError:
    from PySide6 import QtCore, QtWidgets, QtGui
    from shiboken6 import wrapInstance


# ---------------------------------------------------------------------------
# 默认值 (集中管理,打开时会 reset 回这些值)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "asset":      "polymesh",
    "start":      1,
    "pad":        2,
    "hi_suffix":  "high",
    "lo_suffix":  "low",
    "hi_group":   "high",
    "lo_group":   "low",
    "precision":  0.50,
    "tolerance":  1.00,
    "threshold":  1.50,
}


# 变形器白名单:下游连到这些类型的 intermediate shape 一定保留
DEFORMER_TYPES = {
    "skinCluster", "blendShape", "cluster", "ffd", "wrap",
    "nonLinear", "softMod", "tweak", "deltaMush", "shrinkWrap",
    "wire", "sculpt", "textureDeformer", "proximityWrap",
    "jiggle", "morph",
}

# 其它保留类型(渲染集 / 组件引用)
KEEP_IF_DOWNSTREAM = {"groupParts", "objectSet", "shadingEngine"}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is not None:
        return wrapInstance(int(ptr), QtWidgets.QWidget)
    return None


def get_selected_mesh_transforms():
    sel = cmds.ls(selection=True, long=True) or []
    if not sel:
        return []

    result = []
    seen = set()

    def add(node):
        if node and node not in seen:
            seen.add(node)
            result.append(node)

    for node in sel:
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True, type="mesh") or []
        if shapes:
            add(node)
            continue
        if cmds.nodeType(node) == "mesh":
            parent = cmds.listRelatives(node, parent=True, fullPath=True)
            if parent:
                add(parent[0])
            continue
        descendants = cmds.listRelatives(node, allDescendents=True, fullPath=True, type="mesh") or []
        for shp in descendants:
            parent = cmds.listRelatives(shp, parent=True, fullPath=True)
            if parent:
                add(parent[0])

    return result


def natural_key(name):
    short = name.rsplit("|", 1)[-1]
    parts = re.split(r"(\d+)", short)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def reorder_group_children_by_name(group):
    """按名字自然排序组下直接 transform 子节点的 Outliner 顺序。
    只处理 transform,避免动到约束 / locator / groupParts 等节点。
    """
    if not group or not cmds.objExists(group):
        return 0
    children = cmds.listRelatives(group, children=True, fullPath=True, type="transform") or []
    if len(children) < 2:
        return 0

    ordered = sorted(children, key=natural_key)
    freed = []
    for c in ordered:
        try:
            freed.append(cmds.parent(c, world=True)[0])
        except Exception:
            freed.append(c)

    reparented = []
    for c in freed:
        try:
            reparented.append(cmds.parent(c, group)[0])
        except Exception:
            pass
    return len(reparented)


# ---------------------------------------------------------------------------
# 孤立 intermediate shape 清理 (安全删除多余 shape)
# ---------------------------------------------------------------------------

def _is_orphan_intermediate(shape):
    """判断 shape 是否是"可安全删除的孤立 intermediate"。"""
    try:
        if not cmds.getAttr(shape + ".intermediateObject"):
            return False
    except Exception:
        return False

    ins = cmds.listConnections(shape, source=True, destination=False,
                               plugs=False) or []
    if ins:
        return False

    outs = cmds.listConnections(shape, source=False, destination=True,
                                plugs=False) or []
    for o in outs:
        try:
            t = cmds.nodeType(o)
        except Exception:
            return False
        if t in DEFORMER_TYPES or t in KEEP_IF_DOWNSTREAM:
            return False

    return True


def clean_orphan_intermediates(transforms):
    """对给定 transform 列表清理孤立 intermediate shape。
    返回 (清理数量, 每个 transform 的清理明细 dict)。"""
    removed_total = 0
    detail = {}
    for t in transforms:
        try:
            shapes = cmds.listRelatives(t, shapes=True, fullPath=True,
                                        noIntermediate=False) or []
        except Exception:
            continue
        removed_here = []
        for s in shapes:
            if _is_orphan_intermediate(s):
                try:
                    cmds.delete(s)
                    removed_here.append(s.rsplit("|", 1)[-1])
                except Exception:
                    pass
        if removed_here:
            detail[t.rsplit("|", 1)[-1]] = removed_here
            removed_total += len(removed_here)
    return removed_total, detail

# ---------------------------------------------------------------------------
# 配对器
# ---------------------------------------------------------------------------

class PairFinder(object):
    def __init__(self, precision=0.01, tolerance=0.2, face_threshold=1.5):
        self.precision = max(precision, 1e-6)
        self.tolerance = max(tolerance, 0.0)
        self.face_threshold = max(face_threshold, 1.0)

    @staticmethod
    def _bbox_center(node):
        # 加异常保护:无渲染 shape / reference 锁节点等极端情况下返回 None
        try:
            bb = cmds.exactWorldBoundingBox(node)
        except Exception:
            return None
        return ((bb[0] + bb[3]) * 0.5, (bb[1] + bb[4]) * 0.5, (bb[2] + bb[5]) * 0.5)

    @staticmethod
    def _face_count(node):
        try:
            return cmds.polyEvaluate(node, face=True) or 0
        except Exception:
            return 0

    def _quantize(self, center):
        p = self.precision
        return (round(center[0] / p), round(center[1] / p), round(center[2] / p))

    def find_pairs(self, nodes):
        if len(nodes) < 2:
            return [], list(nodes)

        info = []
        skipped = []
        for n in nodes:
            c = self._bbox_center(n)
            if c is None:
                skipped.append(n)
                continue
            info.append({
                "node": n,
                "center": c,
                "faces": self._face_count(n),
            })

        buckets = defaultdict(list)
        for item in info:
            buckets[self._quantize(item["center"])].append(item)

        pairs = []
        used = set()

        for key, items in buckets.items():
            if len(items) < 2:
                continue
            items_sorted = sorted(items, key=lambda x: -x["faces"])
            i = 0
            while i + 1 < len(items_sorted):
                a, b = items_sorted[i], items_sorted[i + 1]
                if a["node"] in used or b["node"] in used:
                    i += 1
                    continue
                pairs.append(self._order_pair(a, b))
                used.add(a["node"])
                used.add(b["node"])
                i += 2

        remain = [x for x in info if x["node"] not in used]
        if self.tolerance > 0 and len(remain) >= 2:
            remain_sorted = sorted(remain, key=lambda x: -x["faces"])
            tol2 = self.tolerance * self.tolerance
            consumed = set()
            for i, a in enumerate(remain_sorted):
                if a["node"] in consumed:
                    continue
                best = None
                best_d2 = tol2
                for j in range(i + 1, len(remain_sorted)):
                    b = remain_sorted[j]
                    if b["node"] in consumed:
                        continue
                    dx = a["center"][0] - b["center"][0]
                    dy = a["center"][1] - b["center"][1]
                    dz = a["center"][2] - b["center"][2]
                    d2 = dx * dx + dy * dy + dz * dz
                    if d2 <= best_d2:
                        best_d2 = d2
                        best = b
                if best is not None:
                    pairs.append(self._order_pair(a, best))
                    consumed.add(a["node"])
                    consumed.add(best["node"])
            used |= consumed

        orphans = [x["node"] for x in info if x["node"] not in used]
        orphans.extend(skipped)
        return pairs, orphans

    def _order_pair(self, a, b):
        fa, fb = a["faces"], b["faces"]
        if fa == 0 and fb == 0:
            high, low = a, b
        elif fa >= fb * self.face_threshold:
            high, low = a, b
        elif fb >= fa * self.face_threshold:
            high, low = b, a
        else:
            high, low = (a, b) if fa >= fb else (b, a)
        return (high["node"], low["node"], high["faces"], low["faces"])


# ---------------------------------------------------------------------------
# 重命名执行
# ---------------------------------------------------------------------------

class Renamer(object):
    def __init__(self, asset_name, start_index, pad, high_suffix, low_suffix,
                 high_group, low_group):
        self.asset_name = asset_name.strip() or "polymesh"
        self.start_index = max(int(start_index), 0)
        self.pad = max(int(pad), 1)
        self.high_suffix = high_suffix.strip() or "high"
        self.low_suffix = low_suffix.strip() or "low"
        self.high_group = high_group.strip() or "high"
        self.low_group = low_group.strip() or "low"

    def _unique_group(self, base):
        """组重名则新建 base1 / base2 …,返回 long path。"""
        if not cmds.objExists(base):
            grp = cmds.group(empty=True, name=base)
        else:
            i = 1
            while cmds.objExists("{0}{1}".format(base, i)):
                i += 1
            grp = cmds.group(empty=True, name="{0}{1}".format(base, i))
        # 返回 long path,避免后续 parent 时短名冲突
        long_list = cmds.ls(grp, long=True) or [grp]
        return long_list[0]

    def _safe_rename(self, node, new_name):
        """用 long path 判断是否已经是目标名,避免短名冲突时误跳过。"""
        if not cmds.objExists(node):
            return node
        long_path = cmds.ls(node, long=True) or []
        if not long_path:
            return node
        current_short = long_path[0].rsplit("|", 1)[-1]
        others = cmds.ls(new_name, long=True) or []
        if current_short == new_name and len(others) <= 1:
            return long_path[0]
        return cmds.rename(node, new_name)

    def run(self, pairs, log):
        if not pairs:
            log("没有可执行的配对。")
            return

        buffered = []

        cmds.refresh(suspend=True)
        try:
            hi_grp = self._unique_group(self.high_group)
            lo_grp = self._unique_group(self.low_group)

            for i, (high, low, fh, fl) in enumerate(pairs):
                idx = self.start_index + i
                num = str(idx).zfill(self.pad)
                hi_name = "{0}{1}_{2}".format(self.asset_name, num, self.high_suffix)
                lo_name = "{0}{1}_{2}".format(self.asset_name, num, self.low_suffix)

                new_hi = self._safe_rename(high, hi_name)
                new_lo = self._safe_rename(low, lo_name)

                try:
                    new_hi = cmds.parent(new_hi, hi_grp)[0]
                except Exception:
                    pass
                try:
                    new_lo = cmds.parent(new_lo, lo_grp)[0]
                except Exception:
                    pass

                buffered.append("[{0}] {1} ({2}面) / {3} ({4}面)".format(
                    num, hi_name, fh, lo_name, fl))

            buffered.append("完成。高模组: {0},低模组: {1}".format(
                hi_grp.rsplit("|", 1)[-1], lo_grp.rsplit("|", 1)[-1]))
        finally:
            cmds.refresh(suspend=False)

        if buffered:
            log("\n".join(buffered))

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class SectionHeader(QtWidgets.QWidget):
    def __init__(self, title, parent=None):
        super(SectionHeader, self).__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(0)
        lbl = QtWidgets.QLabel(title)
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        lay.addWidget(lbl)
        lay.addWidget(line)


class MainDialog(QtWidgets.QDialog):
    _instance = None

    WINDOW_TITLE = "高低模批量命名工具 v5.7"
    LABEL_WIDTH = 70

    @classmethod
    def show_dialog(cls):
        if cls._instance is None:
            cls._instance = cls(parent=maya_main_window())
        else:
            try:
                cls._instance.objectName()
            except RuntimeError:
                cls._instance = cls(parent=maya_main_window())

        cls._instance._reset_inputs()
        cls._instance._clear_log()
        cls._instance._refresh_count()

        cls._instance.show()
        cls._instance.raise_()
        cls._instance.activateWindow()

    def __init__(self, parent=None):
        super(MainDialog, self).__init__(parent)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setFixedWidth(400)
        self.setWindowFlags(self.windowFlags() ^ QtCore.Qt.WindowContextHelpButtonHint)

        self._build_ui()
        self._connect()
        self._reset_inputs()

    def closeEvent(self, event):
        self._clear_log()
        super(MainDialog, self).closeEvent(event)

    def _reset_inputs(self):
        self.ed_asset.setText(DEFAULTS["asset"])
        self.sp_start.setValue(DEFAULTS["start"])
        self.sp_pad.setValue(DEFAULTS["pad"])
        self.ed_hi_suf.setText(DEFAULTS["hi_suffix"])
        self.ed_lo_suf.setText(DEFAULTS["lo_suffix"])
        self.ed_hi_grp.setText(DEFAULTS["hi_group"])
        self.ed_lo_grp.setText(DEFAULTS["lo_group"])
        self.sp_prec.setValue(DEFAULTS["precision"])
        self.sp_tol.setValue(DEFAULTS["tolerance"])
        self.sp_thr.setValue(DEFAULTS["threshold"])

    def _make_label(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setFixedWidth(self.LABEL_WIDTH)
        return lbl

    def _make_spin(self, mn, mx, val, decimals=2, step=0.01):
        sp = QtWidgets.QDoubleSpinBox()
        sp.setDecimals(decimals)
        sp.setRange(mn, mx)
        sp.setSingleStep(step)
        sp.setValue(val)
        sp.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        return sp

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        root.addWidget(SectionHeader("资产"))

        row_asset = QtWidgets.QHBoxLayout()
        row_asset.addWidget(self._make_label("资产名"))
        self.ed_asset = QtWidgets.QLineEdit()
        row_asset.addWidget(self.ed_asset)
        root.addLayout(row_asset)

        row_num = QtWidgets.QHBoxLayout()
        row_num.addWidget(self._make_label("起始编号"))
        self.sp_start = QtWidgets.QSpinBox()
        self.sp_start.setRange(0, 99999)
        self.sp_start.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_num.addWidget(self.sp_start)
        row_num.addSpacing(8)
        row_num.addWidget(self._make_label("编号位数"))
        self.sp_pad = QtWidgets.QSpinBox()
        self.sp_pad.setRange(1, 6)
        self.sp_pad.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_num.addWidget(self.sp_pad)
        root.addLayout(row_num)

        row_suf = QtWidgets.QHBoxLayout()
        row_suf.addWidget(self._make_label("高模后缀"))
        self.ed_hi_suf = QtWidgets.QLineEdit()
        self.ed_hi_suf.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_suf.addWidget(self.ed_hi_suf)
        row_suf.addSpacing(8)
        row_suf.addWidget(self._make_label("低模后缀"))
        self.ed_lo_suf = QtWidgets.QLineEdit()
        self.ed_lo_suf.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_suf.addWidget(self.ed_lo_suf)
        root.addLayout(row_suf)

        row_grp = QtWidgets.QHBoxLayout()
        row_grp.addWidget(self._make_label("高模组名"))
        self.ed_hi_grp = QtWidgets.QLineEdit()
        self.ed_hi_grp.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_grp.addWidget(self.ed_hi_grp)
        row_grp.addSpacing(8)
        row_grp.addWidget(self._make_label("低模组名"))
        self.ed_lo_grp = QtWidgets.QLineEdit()
        self.ed_lo_grp.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_grp.addWidget(self.ed_lo_grp)
        root.addLayout(row_grp)

        root.addWidget(SectionHeader("配对"))
        row_pair = QtWidgets.QHBoxLayout()
        row_pair.addWidget(QtWidgets.QLabel("精度"))
        self.sp_prec = self._make_spin(0.0001, 10.0, DEFAULTS["precision"])
        row_pair.addWidget(self.sp_prec)
        row_pair.addSpacing(6)
        row_pair.addWidget(QtWidgets.QLabel("容差"))
        self.sp_tol = self._make_spin(0.0, 100.0, DEFAULTS["tolerance"])
        row_pair.addWidget(self.sp_tol)
        row_pair.addSpacing(6)
        row_pair.addWidget(QtWidgets.QLabel("阈值"))
        self.sp_thr = self._make_spin(1.0, 100.0, DEFAULTS["threshold"])
        row_pair.addWidget(self.sp_thr)
        root.addLayout(row_pair)

        root.addWidget(SectionHeader("选择"))
        row_cnt = QtWidgets.QHBoxLayout()
        self.lbl_count = QtWidgets.QLabel("有效 mesh: 0")
        row_cnt.addWidget(self.lbl_count)
        row_cnt.addStretch()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.setFixedWidth(60)
        row_cnt.addWidget(self.btn_refresh)
        root.addLayout(row_cnt)

        root.addWidget(SectionHeader("日志"))
        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        f = QtGui.QFont("Consolas", 9)
        self.txt_log.setFont(f)
        self.txt_log.setFixedHeight(160)
        root.addWidget(self.txt_log)

        row_main = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("预览")
        self.btn_run = QtWidgets.QPushButton("执行")
        self.btn_close = QtWidgets.QPushButton("关闭")
        for b in (self.btn_preview, self.btn_run, self.btn_close):
            row_main.addWidget(b)
        root.addLayout(row_main)

        row_sel = QtWidgets.QHBoxLayout()
        self.btn_sel_hi = QtWidgets.QPushButton("选中高模")
        self.btn_sel_lo = QtWidgets.QPushButton("选中低模")
        self.btn_sel_all = QtWidgets.QPushButton("全选")
        self.btn_sort = QtWidgets.QPushButton("名称排序")
        for b in (self.btn_sel_hi, self.btn_sel_lo, self.btn_sel_all, self.btn_sort):
            row_sel.addWidget(b)
        root.addLayout(row_sel)

    def _connect(self):
        self.btn_refresh.clicked.connect(self._refresh_count)
        self.btn_preview.clicked.connect(self._on_preview)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_close.clicked.connect(self.close)
        self.btn_sel_hi.clicked.connect(lambda: self._select_group(self.ed_hi_grp.text().strip()))
        self.btn_sel_lo.clicked.connect(lambda: self._select_group(self.ed_lo_grp.text().strip()))
        self.btn_sel_all.clicked.connect(self._select_both_groups)
        self.btn_sort.clicked.connect(self._on_sort)

    def log(self, msg):
        self.txt_log.appendPlainText(msg)
        QtWidgets.QApplication.processEvents()

    def _clear_log(self):
        self.txt_log.clear()

    def _refresh_count(self):
        nodes = get_selected_mesh_transforms()
        self.lbl_count.setText("有效 mesh: {0}".format(len(nodes)))
        return nodes

    def _make_finder(self):
        return PairFinder(
            precision=self.sp_prec.value(),
            tolerance=self.sp_tol.value(),
            face_threshold=self.sp_thr.value(),
        )

    def _make_renamer(self):
        return Renamer(
            asset_name=self.ed_asset.text(),
            start_index=self.sp_start.value(),
            pad=self.sp_pad.value(),
            high_suffix=self.ed_hi_suf.text(),
            low_suffix=self.ed_lo_suf.text(),
            high_group=self.ed_hi_grp.text(),
            low_group=self.ed_lo_grp.text(),
        )

    def _preprocess(self, nodes):
        """预览 / 执行前静默清理孤立 intermediate shape。"""
        if not nodes:
            return
        count, detail = clean_orphan_intermediates(nodes)
        if count > 0:
            self.log("预处理: 已清理 {0} 个残留 intermediate shape".format(count))
            names = list(detail.items())
            for tname, shapes in names[:5]:
                self.log("  {0}: {1}".format(tname, ", ".join(shapes)))
            if len(names) > 5:
                self.log("  ... 其余 {0} 个模型略".format(len(names) - 5))

    def _on_preview(self):
        self._clear_log()
        nodes = self._refresh_count()
        if len(nodes) < 2:
            self.log("请先选中至少 2 个 mesh / 组。")
            return
        self.log("输入 mesh 数量: {0}".format(len(nodes)))

        cmds.undoInfo(openChunk=True, chunkName="HighLowRenamer-Prep")
        try:
            self._preprocess(nodes)
        finally:
            cmds.undoInfo(closeChunk=True)

        pairs, orphans = self._make_finder().find_pairs(nodes)
        lines = ["=== 预览配对结果 ({0} 对) ===".format(len(pairs))]
        for h, l, fh, fl in pairs:
            hs = h.rsplit("|", 1)[-1]
            ls = l.rsplit("|", 1)[-1]
            lines.append("  {0} ({1}面)  <=>  {2} ({3}面)".format(hs, fh, ls, fl))
        if orphans:
            lines.append("--- 警告 ---")
            lines.append("跳过孤儿模型 {0} 个:".format(len(orphans)))
            for o in orphans:
                lines.append("  " + o.rsplit("|", 1)[-1])
        self.log("\n".join(lines))

    def _on_run(self):
        self._clear_log()
        nodes = self._refresh_count()
        if len(nodes) < 2:
            self.log("请先选中至少 2 个 mesh / 组。")
            return
        self.log("输入 mesh 数量: {0}".format(len(nodes)))

        cmds.undoInfo(openChunk=True, chunkName="HighLowRenamer-Run")
        try:
            self._preprocess(nodes)
            pairs, orphans = self._make_finder().find_pairs(nodes)
            self.log("配对 {0} 对,孤儿 {1} 个。".format(len(pairs), len(orphans)))
            if pairs:
                self._make_renamer().run(pairs, self.log)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._refresh_count()

    def _select_group(self, grp):
        if not grp or not cmds.objExists(grp):
            self.log("组不存在: {0}".format(grp))
            return
        children = cmds.listRelatives(grp, allDescendents=True, fullPath=True, type="mesh") or []
        transforms = []
        seen = set()
        for s in children:
            p = cmds.listRelatives(s, parent=True, fullPath=True)
            if p and p[0] not in seen:
                seen.add(p[0])
                transforms.append(p[0])
        if transforms:
            cmds.select(transforms, replace=True)
            self.log("已选中 {0} 下 {1} 个 mesh。".format(grp, len(transforms)))
        self._refresh_count()

    def _select_both_groups(self):
        targets = []
        for g in (self.ed_hi_grp.text().strip(), self.ed_lo_grp.text().strip()):
            if g and cmds.objExists(g):
                targets.append(g)
        if not targets:
            self.log("高模组 / 低模组都不存在。")
            return
        cmds.select(clear=True)
        for g in targets:
            descendants = cmds.listRelatives(g, allDescendents=True, fullPath=True, type="mesh") or []
            transforms = []
            seen = set()
            for s in descendants:
                p = cmds.listRelatives(s, parent=True, fullPath=True)
                if p and p[0] not in seen:
                    seen.add(p[0])
                    transforms.append(p[0])
            if transforms:
                cmds.select(transforms, add=True)
        self.log("已选中高模组 + 低模组下全部 mesh。")
        self._refresh_count()

    def _on_sort(self):
        self._clear_log()
        total = 0
        for g in (self.ed_hi_grp.text().strip(), self.ed_lo_grp.text().strip()):
            if g and cmds.objExists(g):
                n = reorder_group_children_by_name(g)
                self.log("已排序 {0}:{1} 项".format(g, n))
                total += n
        if total == 0:
            self.log("没有可排序的组(高模组 / 低模组不存在)。")


def show_ui():
    MainDialog.show_dialog()


def show():
    MainDialog.show_dialog()


if __name__ == "__main__":
    show_ui()