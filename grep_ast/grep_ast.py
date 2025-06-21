#!/usr/bin/env python

import re
from fuzzywuzzy import fuzz, process

from .dump import dump  # noqa: F401
from .parsers import filename_to_lang
from .tsl import get_parser


class TreeContext:
    def __init__(
        self,
        filename,
        code,
        color=False,
        verbose=False,
        line_number=False,
        parent_context=True,
        child_context=True,
        last_line=True,
        margin=3,
        mark_lois=True,
        header_max=10,
        show_top_of_file_parent_scope=True,
        loi_pad=1,
    ):
        self.filename = filename
        self.color = color
        self.verbose = verbose
        self.line_number = line_number
        self.last_line = last_line
        self.margin = margin
        self.mark_lois = mark_lois
        self.header_max = header_max
        self.loi_pad = loi_pad
        self.show_top_of_file_parent_scope = show_top_of_file_parent_scope

        self.parent_context = parent_context
        self.child_context = child_context

        lang = filename_to_lang(filename)
        if not lang:
            raise ValueError(f"Unknown language for {filename}")

        # Get parser based on file extension
        parser = get_parser(lang)
        tree = parser.parse(bytes(code, "utf8"))

        self.lines = code.splitlines()
        self.num_lines = len(self.lines) + 1

        # color lines, with highlighted matches
        self.output_lines = dict()

        # Which scopes is each line part of?
        # A scope is the line number on which the scope started
        self.scopes = [set() for _ in range(self.num_lines)]

        # Which lines serve as a short "header" for the scope starting on that line
        self.header = [list() for _ in range(self.num_lines)]

        self.nodes = [list() for _ in range(self.num_lines)]

        root_node = tree.root_node
        self.walk_tree(root_node)

        if self.verbose:
            scope_width = max(len(str(set(self.scopes[i]))) for i in range(self.num_lines - 1))
        for i in range(self.num_lines):
            header = sorted(self.header[i])
            if self.verbose and i < self.num_lines - 1:
                scopes = str(sorted(set(self.scopes[i])))
                print(f"{scopes.ljust(scope_width)}", i, self.lines[i])

            if len(header) > 1:
                size, head_start, head_end = header[0]
                if size > self.header_max:
                    head_end = head_start + self.header_max
            else:
                head_start = i
                head_end = i + 1

            self.header[i] = head_start, head_end

        self.show_lines = set()
        self.lines_of_interest = set()

        return

    def grep(self, pat, ignore_case, fuzzy_threshold=80):
        """
        Search lines for exact regex matches and fuzzy matches above the threshold.

        Args:
            pat (str): The regex pattern to search for.
            ignore_case (bool): Whether to ignore case in regex search.
            fuzzy_threshold (int): Minimum similarity ratio (0-100) for fuzzy matches.

        Returns:
            set: Line numbers where either regex or fuzzy matches were found.
        """
        found = set()
        flags = re.IGNORECASE if ignore_case else 0
        for i, line in enumerate(self.lines):
            regex_match = re.search(pat, line, flags)
            fuzzy_match = False
            # Check fuzzy match against each word in the line
            for word in re.findall(r"\w+", line):
                ratio = fuzz.ratio(pat.lower(), word.lower())
                if ratio >= fuzzy_threshold:
                    fuzzy_match = True
                    break

            if regex_match or fuzzy_match:
                # Highlight matches when color is enabled
                if self.color:
                    highlighted = line
                    if regex_match:
                        highlighted = re.sub(
                            pat,
                            lambda m: f"\033[1;31m{m.group()}\033[0m",
                            highlighted,
                            flags=flags,
                        )
                    if fuzzy_match:
                        for word in re.findall(r"\w+", line):
                            if fuzz.ratio(pat.lower(), word.lower()) >= fuzzy_threshold:
                                highlighted = re.sub(
                                    rf"({word})",
                                    lambda m: f"\033[1;33m{m.group()}\033[0m",
                                    highlighted,
                                )
                    self.output_lines[i] = highlighted
                found.add(i)
        return found

    def add_lines_of_interest(self, line_nums):
        self.lines_of_interest.update(line_nums)

    def add_context(self):
        if not self.lines_of_interest:
            return

        self.done_parent_scopes = set()
        self.show_lines = set(self.lines_of_interest)

        if self.loi_pad:
            for line in list(self.show_lines):
                for new_line in range(line - self.loi_pad, line + self.loi_pad + 1):
                    if new_line >= self.num_lines or new_line < 0:
                        continue
                    self.show_lines.add(new_line)

        if self.last_line:
            bottom_line = self.num_lines - 2
            self.show_lines.add(bottom_line)
            self.add_parent_scopes(bottom_line)

        if self.parent_context:
            for i in set(self.lines_of_interest):
                self.add_parent_scopes(i)

        if self.child_context:
            for i in set(self.lines_of_interest):
                self.add_child_context(i)

        if self.margin:
            self.show_lines.update(range(self.margin))

        self.close_small_gaps()

    def add_child_context(self, i):
        if not self.nodes[i]:
            return

        last_line = self.get_last_line_of_scope(i)
        size = last_line - i
        if size < 5:
            self.show_lines.update(range(i, last_line + 1))
            return

        children = []
        for node in self.nodes[i]:
            children += self.find_all_children(node)

        children = sorted(
            children,
            key=lambda node: node.end_point[0] - node.start_point[0],
            reverse=True,
        )

        currently_showing = len(self.show_lines)
        max_to_show = max(min(size * 0.10, 25), 5)

        for child in children:
            if len(self.show_lines) > currently_showing + max_to_show:
                break
            child_start_line = child.start_point[0]
            self.add_parent_scopes(child_start_line)

    def find_all_children(self, node):
        children = [node]
        for child in node.children:
            children += self.find_all_children(child)
        return children

    def get_last_line_of_scope(self, i):
        return max(node.end_point[0] for node in self.nodes[i])

    def close_small_gaps(self):
        closed_show = set(self.show_lines)
        sorted_show = sorted(self.show_lines)
        for idx in range(len(sorted_show) - 1):
            if sorted_show[idx + 1] - sorted_show[idx] == 2:
                closed_show.add(sorted_show[idx] + 1)

        for i, line in enumerate(self.lines):
            if i not in closed_show:
                continue
            if self.lines[i].strip() and i < self.num_lines - 2 and not self.lines[i + 1].strip():
                closed_show.add(i + 1)

        self.show_lines = closed_show

    def format(self):
        if not self.show_lines:
            return ""

        output = ""
        if self.color:
            output += "\033[0m\n"

        dots = not (0 in self.show_lines)
        for i, line in enumerate(self.lines):
            if i not in self.show_lines:
                if dots:
                    output += (f"{i + 1: 3}...⋮...\n" if self.line_number else "⋮\n")
                    dots = False
                continue

            spacer = "█" if (i in self.lines_of_interest and self.mark_lois) else "│"
            if self.color and spacer == "█":
                spacer = f"\033[31m{spacer}\033[0m"

            line_output = f"{spacer}{self.output_lines.get(i, line)}"
            if self.line_number:
                line_output = f"{i + 1: 3}" + line_output
            output += line_output + "\n"
            dots = True

        return output

    def add_parent_scopes(self, i):
        if i in self.done_parent_scopes:
            return
        self.done_parent_scopes.add(i)

        if i >= len(self.scopes):
            return

        for line_num in self.scopes[i]:
            head_start, head_end = self.header[line_num]
            if head_start > 0 or self.show_top_of_file_parent_scope:
                self.show_lines.update(range(head_start, head_end))

            if self.last_line:
                last_line = self.get_last_line_of_scope(line_num)
                self.add_parent_scopes(last_line)

    def walk_tree(self, node, depth=0):
        start_line, end_line = node.start_point[0], node.end_point[0]
        size = end_line - start_line

        self.nodes[start_line].append(node)
        if self.verbose and node.is_named:
            print(
                "   " * depth,
                node.type,
                f"{start_line}-{end_line}={size + 1}",
                node.text.splitlines()[0],
                self.lines[start_line],
            )

        if size:
            self.header[start_line].append((size, start_line, end_line))

        for i in range(start_line, end_line + 1):
            self.scopes[i].add(start_line)

        for child in node.children:
            self.walk_tree(child, depth + 1)

        return start_line, end_line
