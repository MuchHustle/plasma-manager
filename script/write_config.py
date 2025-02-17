import glob
import json
import os
import re
import sys
import os
from dataclasses import dataclass
from typing import Dict, Optional, Self, Set


# KDE has a bespoke escape format:
# https://github.com/KDE/kconfig/blob/44f98ff5cb9008436ba5ba385cae03bbd0ab33e6/src/core/kconfigini.cpp#L882
def unescape(s: str) -> str:
    out = []
    while s:
        parts = s.split("\\", 1)
        out.append(parts.pop(0))
        if not parts:
            break
        s = parts[0]
        if not s:
            out.append("\\")
            break
        symbol, s = s[0], s[1:]
        match symbol:
            case "s":
                out.append(" ")
            case "t":
                out.append("\t")
            case "n":
                out.append("\n")
            case "r":
                out.append("\r")
            case "\\":
                out.append("\\")
            case ";":
                out.append("\\;")
            case ",":
                out.append("\\,")
            case "x" if len(s) >= 2:
                num = s[0:2]
                try:
                    out.append(chr(int(num, 16)))
                    s = s[2:]
                except ValueError:
                    out.append("\\x")
            case _:
                # Invalid escape sequence
                out.append("\\" + symbol)
    return "".join(out)


def escape_bytes(c: str) -> str:
    return "".join(f"\\x{b:02x}" for b in c.encode("utf-8"))


def escape(s: str) -> str:
    if not s:
        return s
    s = list(s)
    for i, c in enumerate(s):
        match c:
            case "\n":
                s[i] = "\\n"
            case "\t":
                s[i] = "\\t"
            case "\r":
                s[i] = "\\r"
            case "\\":
                s[i] = "\\\\"
            case "=" | "[" | "]":
                s[i] = escape_bytes(c)
            case _ if ord(c) < 32:
                s[i] = escape_bytes(c)
    for i in (0, -1):
        if s[i] == " ":
            s[i] = "\\s"
    return "".join(s)

# Some values need to be "transformed", such as being converted from hex to decimal
def transformValues(d_orig):
    d = d_orig.copy()

    c_path = os.path.expanduser("~/.config/kcminputrc")
    if c_path in d:
        for item_str in list(d[c_path]):
            if item_str.startswith("Libinput/"):
                item = d[c_path].pop(item_str)
                item_str_list = item_str.split("/")
                item_str_list[1] = str(int(item_str_list[1],16))
                item_str_list[2] = str(int(item_str_list[2],16))
                item_str = "/".join(item_str_list)
                d[c_path][item_str] = item
    return d;

@dataclass
class ConfigValue:
    value: Optional[str]
    immutable: bool = False
    shellExpand: bool = False

    @classmethod
    def parse_line(cls, line: str) -> tuple[str, Self]:
        line_splitted = line.split("=", 1)
        key = line_splitted.pop(0).strip()
        marking = ""
        if "[" in key:
            key, marking = key.split("[")
            marking = marking[1:-1]
        value = cls(
            value=None,
            immutable="i" in marking,
            shellExpand="e" in marking,
        )
        key = unescape(key)
        if line_splitted:
            value.value = line_splitted[0].strip()
        return key, value

    @classmethod
    def from_json(cls, value: dict) -> Self:
        key_value = (
            str(value["value"])
            if not isinstance(value["value"], bool)
            else str(value["value"]).lower()
        )
        return cls(
            value=escape(key_value),
            immutable=value["immutable"],
            shellExpand=value["shellExpand"],
        )

    @property
    def marking(self):
        """
        Calculates the "marking" we should add to the keys, which for example may be
        [$i] if we want immutability, or [$e] if we want to expand variables. See
        https://api.kde.org/frameworks/kconfig/html/options.html for some options.
        """
        if self.immutable and self.shellExpand:
            return "[$ei]"
        elif self.immutable:
            return "[$i]"
        elif self.shellExpand:
            return "[$e]"
        else:
            return ""

    def to_line(self, key: str) -> str:
        """For keys with values (not None) we give key=value, if not just give
        the key as the line (this is useful in khotkeysrc)."""
        key = escape(key) + self.marking
        return f"{key}={self.value}" if self.value is not None else key


class KConfManager:
    def __init__(
        self, filepath: str, json_dict: Dict, reset: bool, immutable_by_default: bool
    ):
        """
        filepath (str): The full path to the config-file to manage
        json_dict (Dict): The nix-configuration presented in a dictionary (converted from json)
        reset (bool): Whether to reset the file, i.e. remove all the lines not present in the configuration
        """
        self.data = {}
        self.json_dict = json_dict
        self.filepath = filepath
        self.reset = reset
        self.immutable_by_default = immutable_by_default
        self._json_value_checks()
        # The nix expressions will have / to separate groups, and \/ to escape a /.
        # This parses the groups into tuples of unescaped group names.
        self.json_dict = {
            tuple(
                g.replace("\\/", "/")
                for g in re.findall(r"(/|(?:[^/\\]|\\.)+)", group)[::2]
            ): entry
            for group, entry in self.json_dict.items()
        }

    def _json_value_checks(self):
        for group, entry in self.json_dict.items():
            for key, value in entry.items():
                non_default_immutability = (
                    value["immutable"] != self.immutable_by_default
                )
                if (
                    value["value"] is None
                    and not value["persistent"]
                    and (non_default_immutability or value["shellExpand"])
                ):
                    raise Exception(
                        f'Plasma-manager: No value or persistency set for key "{key}" in group "{group}" in configfile "{self.filepath}"'
                        ", but one of immutability/persistency takes a non-default value. This is not supported"
                    )
                elif value["persistent"]:
                    base_msg = f'Plasma-manager: Persistency enabled for key "{key}" in group "{group}" in configfile "{self.filepath}"'
                    if value["value"] is not None:
                        raise Exception(
                            f"{base_msg} with non-null value \"{value['value']}\". "
                            "A value cannot be given when persistency is enabled"
                        )
                    elif non_default_immutability:
                        raise Exception(
                            f"{base_msg} with non-default immutability. Persistency with non-default immutability is not supported"
                        )
                    elif value["shellExpand"]:
                        raise Exception(
                            f"{base_msg} with shell-expansion enabled. Persistency with shell-expansion enabled is not supported"
                        )

    def key_is_persistent(self, group, key) -> bool:
        """
        Checks if a key in a group in the nix config is persistent.
        """
        try:
            is_persistent = (
                self.json_dict[group][key]["persistent"]
                and self.json_dict[group][key]["value"] is None
            )
        except KeyError:
            is_persistent = False

        return is_persistent

    def read(self):
        """
        Read the config from the path specified on instantiation. If the
        path doesn't exist, do nothing.
        """
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                current_group = ()  # default group
                for l in f:
                    # Checks if the current line indicates a group.
                    if re.match(r"^\[.*\]\s*$", l):
                        current_group = l.rstrip()[1:-1].split("][")
                        current_group = tuple(unescape(g) for g in current_group)
                        self.data[current_group] = {}
                        continue

                    # We won't bother reading empty lines.
                    if l.strip() != "":
                        key, value = ConfigValue.parse_line(l)
                        is_persistent = self.key_is_persistent(current_group, key)
                        should_keep_key = is_persistent or not self.reset
                        if should_keep_key:
                            self.set_value(
                                current_group,
                                key,
                                value,
                            )
        except FileNotFoundError:
            pass

    def run(self):
        self.read()
        for group, entry in self.json_dict.items():
            for key, value in entry.items():
                # If the nix expression is null, resulting in the value None here,
                # we remove the key/option (and the group/section if it is empty
                # after removal, and persistency is disabled).
                if value["value"] is None and not value["persistent"]:
                    self.remove_value(group, key)
                    continue

                # Again values from the nix json are not escaped, so we need to
                # escape them here (in case it includes \t \n and so on). We
                # also don't set the keys if the key is persistent, as we want
                # to leave that key unchanged from the read() method.
                if not value["persistent"]:
                    self.set_value(group, key, ConfigValue.from_json(value))

    def set_value(self, group, key, value):
        """Adds an entry to the config. Creates necessary groups if needed."""
        if not group in self.data:
            self.data[group] = {}

        self.data[group][key] = value

    def remove_value(self, group, key):
        """Removes an entry from the config. Does nothing if the entry isn't there."""
        if group in self.data and key in self.data[group]:
            del self.data[group][key]

    def save(self):
        """Save to the filepath specified on instantiation."""
        # If the directory we want to save to doesn't exist we will allow this,
        # and just create the directory before.
        dir = os.path.dirname(self.filepath)
        if not os.path.exists(dir):
            os.makedirs(dir)

        with open(self.filepath, "w", encoding="utf-8") as f:
            # We skip a newline before the first category
            skip_newline = True

            for group in sorted(self.data):
                if not self.data[group]:
                    # We skip over groups with no keys, they don't need to be written
                    continue

                if not skip_newline:
                    f.write("\n")
                else:
                    # Only skip the newline once of course.
                    skip_newline = False

                if group:
                    key = "][".join(escape(g) for g in group)
                    f.write(f"[{key}]\n")
                for key, value in self.data[group].items():
                    f.write(f"{value.to_line(key)}\n")


def remove_config_files(d: Dict, reset_files: Set):
    """
    Removes files which doesn't have any configuration entries in d and which is
    in the list of files to be reset by overrideConfig.
    """
    for del_path in reset_files - set(d.keys()):
        for file_to_del in glob.glob(del_path):
            if os.path.isfile(file_to_del):
                os.remove(file_to_del)


def write_configs(d: Dict, reset_files: Set, immutable_by_default: bool):
    for filepath, c in d.items():
        config = KConfManager(
            filepath, c, filepath in reset_files, immutable_by_default
        )
        config.run()
        config.save()


def main():
    if len(sys.argv) != 4:
        raise ValueError(
            f"Must receive exactly four arguments, got: {len(sys.argv) - 1}"
        )

    json_path = sys.argv[1]
    with open(json_path, "r") as f:
        json_str = f.read()

    reset_files = set(sys.argv[2].split(" ")) if sys.argv[2] != "" else set()
    immutable_by_default = bool(sys.argv[3])
    d_raw = json.loads(json_str)
    d = transformValues(d_raw)
    remove_config_files(d, reset_files)
    write_configs(d, reset_files, immutable_by_default)


if __name__ == "__main__":
    main()
