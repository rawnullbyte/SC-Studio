"""Lazy reverse-reference browser: shared branches stay expandable; cycles stop."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem


class ReferenceTree(QTreeWidget):
    open_resource = pyqtSignal(int)

    def __init__(self, document, target, parent=None):
        super().__init__(parent)
        self.setObjectName("reference_tree")
        self.setHeaderLabels(["Resource / referencing parents", "ID", "Instance"])
        graph = document.reference_graph(target)
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        self.parents = {}
        for edge in graph["edges"]:
            self.parents.setdefault(edge["from"], []).append(edge)
        self.itemExpanded.connect(self.expand_branch)
        self.itemDoubleClicked.connect(self.follow)
        root = self.add_node(self, target, (), "")
        root.setExpanded(True)
        self.setColumnWidth(0, 400)
        self.setToolTip("Expand parents to follow references to their roots. Double-click a resource to open it.")

    def add_node(self, parent, id, ancestry, instance):
        node = self.nodes[id]
        label = " / ".join(node["exports"]) or f"{node['kind'].rstrip('s').title()} {id}"
        cyclic = id in ancestry
        if cyclic:
            label += " · Cycle"
        elif node["terminal"]:
            label += " · Root (no parents)"
        item = QTreeWidgetItem(parent, [label, str(id), instance])
        item.setData(0, Qt.ItemDataRole.UserRole, (id, ancestry + (id,)))
        if not cyclic and self.parents.get(id):
            QTreeWidgetItem(item, ["Expand to load parents…"])
        return item

    def expand_branch(self, item):
        if item.childCount() != 1 or item.child(0).data(0, Qt.ItemDataRole.UserRole) is not None:
            return
        item.takeChild(0)
        id, ancestry = item.data(0, Qt.ItemDataRole.UserRole)
        for edge in self.parents.get(id, []):
            self.add_node(item, edge["to"], ancestry,
                          f"{edge['name']} · child {edge['child']}")

    def follow(self, item, *_):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.open_resource.emit(data[0])
