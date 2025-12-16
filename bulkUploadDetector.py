import libcst as cst
from libcst.metadata import PositionProvider, ParentNodeProvider
from pathlib import Path
import importlib.util
from collections import defaultdict
import os
import sys

class BulkOpDetector(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider)

    def __init__(self, filename, source_lines):
        self.filename = filename
        self.source_lines = source_lines
        self.bulk_candidates = []

        # Track all save/update operations (leaf nodes)
        self.leaf_nodes = []

        # Call graph
        self.call_graph = defaultdict(list)

        # Track function definitions and their calls
        self.function_calls = defaultdict(list)

        # Current context
        self.current_function = None

    def visit_FunctionDef(self, node):
        self.current_function = node.name.value

    def leave_FunctionDef(self, node):
        self.current_function = None

    def visit_Call(self, node):

        # Handle save/update calls (leaf nodes)
        if isinstance(node.func, cst.Attribute):
            method_name = node.func.attr.value
            if method_name in ("save", "update"):
                self._process_save_update(node)

            elif self.current_function:
                called_func = method_name
                call_context = self._get_node_context(node)
                self.function_calls[self.current_function].append({
                    'called': called_func,
                    'context': call_context,
                    'node': node
                })
                if called_func not in self.call_graph[self.current_function]:
                    self.call_graph[self.current_function].append(called_func)

        elif isinstance(node.func, cst.Name) and self.current_function:
            called_func = node.func.value
            call_context = self._get_node_context(node)
            self.function_calls[self.current_function].append({
                'called': called_func,
                'context': call_context,
                'node': node
            })
            if called_func not in self.call_graph[self.current_function]:
                self.call_graph[self.current_function].append(called_func)

    def _process_save_update(self, node):
        pos = self.get_metadata(PositionProvider, node)
        code_lines = self.source_lines[pos.start.line - 1:pos.end.line]
        code_snippet = "\n".join(code_lines).strip()
        context = self._get_node_context(node)

        self.leaf_nodes.append({
            'function': self.current_function,
            'line': pos.start.line,
            'code': code_snippet,
            'context': context
        })

    def _get_node_context(self, node):
        context = []
        parent = self.get_metadata(ParentNodeProvider, node, default=None)

        while parent is not None:
            if isinstance(parent, (cst.For, cst.While)):
                pos = self.get_metadata(PositionProvider, parent)
                code_line = self.source_lines[pos.start.line - 1].strip()
                context.append(f"[loop] {code_line}")
            elif isinstance(parent, cst.FunctionDef):
                context.append(f"{parent.name.value}()")
            elif isinstance(parent, cst.ClassDef):
                context.append(f"Class: {parent.name.value}")
            parent = self.get_metadata(ParentNodeProvider, parent, default=None)

        return list(reversed(context))

    def analyze(self):
        seen = set()

        for leaf in self.leaf_nodes:
            if not leaf['function']:
                continue

            paths = self._build_paths_to_leaf(leaf['function'], leaf)

            for path in paths:
                loop_count = self._has_loops_in_path(path)
                if loop_count == 0:
                    continue

                key = (self.filename, leaf['line'], leaf['code'])
                if key in seen:
                    continue

                seen.add(key)

                self.bulk_candidates.append({
                    "file": self.filename,
                    "indirect_call": None,
                    "line": leaf['line'],
                    "code": leaf['code'],
                    "nesting": " --> ".join(path),
                    "loop_count": loop_count
                })

    def _build_paths_to_leaf(self, func_name, leaf):
        paths = []
        base_path = [leaf['code']]

        if leaf['context']:
            base_path = leaf['context'] + base_path

        self._find_paths_to_function(func_name, [], base_path, paths)

        if not paths:
            paths.append(base_path)

        return paths

    def _find_paths_to_function(self, target_func, visited, base_path, all_paths):
        if target_func in visited:
            return

        callers = [func for func, calls in self.call_graph.items() if target_func in calls]

        if not callers:
            all_paths.append(base_path)
            return

        for caller in callers:
            call_contexts = [
                call['context']
                for call in self.function_calls[caller]
                if call['called'] == target_func
            ]

            for context in call_contexts:
                extended_path = context + base_path
                self._find_paths_to_function(
                    caller,
                    visited + [target_func],
                    extended_path,
                    all_paths
                )

    def _has_loops_in_path(self, path):
        return sum('loop' in str(item).lower() for item in path)


def scan_file_for_bulk_ops(path, root_path):
    try:
        source = path.read_text()
        source_lines = source.splitlines()

        module = cst.parse_module(source)
        wrapper = cst.MetadataWrapper(module)

        detector = BulkOpDetector(str(path), source_lines)
        wrapper.visit(detector)
        detector.analyze()

        return detector.bulk_candidates

    except Exception as e:
        print(f"Error parsing {path}: {e}")
        return []



def find_model_files(root_dir, skip_dirs=None):
    # Skip these folders since not useful.
    if skip_dirs is None:
        skip_dirs = {"node_modules", "pynanolog", "scripts", "staticfiles", "static", 
                     "commands", "myenv", "migrations", "management", "__pycache__"}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        # Join dirpath with file name to give absolute path
        for filename in filenames:
            if filename.endswith('.py'):
                yield os.path.join(dirpath, filename)


if __name__ == "__main__":
    root_path = input("Enter the path to your Django project: ").strip()
    # if root_path not in sys.path:
    #     sys.path.insert(0, root_path)

    # print("Project root added to sys.path:", root_path)

    output_dir = Path("Bulk_candidates")
    output_dir.mkdir(exist_ok=True)

    if not os.path.isdir(root_path):
        print(f"Invalid directory: {root_path}")
    else:
        queriesFlagged = 0
        grouped_candidates = {}

        for path in find_model_files(root_path):
            bulk_ops = scan_file_for_bulk_ops(Path(path), root_path)
            for i, candidate in enumerate(bulk_ops, 1):
                # Store in dictionary grouped by loop nesting level
                grouped_candidates.setdefault(candidate['loop_count'], []).append(candidate)
                
                print(f"\n{queriesFlagged}: File: {candidate['file']}")
                print(f"   Line: {candidate['line']}")
                print(f"   Code: {candidate['code']}")
                print(f"   Path: {candidate['nesting']}")
                print(f"   Loop Nesting: {candidate['loop_count']}")
                # print(f"   Total Paths Found: {candidate.get('total_paths', 1)}")
                queriesFlagged += 1

        # Write result to file based on nesting levels
        for loop_nesting, candidates in grouped_candidates.items():
            output_file_dir = output_dir / f"bulk_candidates_{loop_nesting}.txt"
            with open(output_file_dir, "w") as f:
                for idx, candidate in enumerate(candidates, 1):
                    f.write(f"{idx}. File: {candidate['file']}\n")
                    f.write(f"   Line: {candidate['line']}\n")
                    f.write(f"   Code: {candidate['code']}\n")
                    f.write(f"   Path: {candidate['nesting']}\n")
                    f.write(f"   Loop Nesting: {candidate['loop_count']}\n")
                    # f.write(f"   Total Paths Found: {candidate.get('total_paths', 1)}\n\n")

        print(f'\n  Total queries flagged for bulk operation in codebase: {queriesFlagged}')
        print(f"Results saved in: {output_dir.resolve()}")