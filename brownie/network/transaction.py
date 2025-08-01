#!/usr/bin/python3

import functools
import sys
import threading
import time
from enum import IntEnum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Concatenate,
    Dict,
    List,
    Optional,
    ParamSpec,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)
from warnings import warn

import black
import requests
from eth_abi import decode
from web3.exceptions import TransactionNotFound

from brownie._c_constants import HexBytes, deque, regex_compile, sha1
from brownie._config import CONFIG
from brownie.convert import EthAddress, Wei
from brownie.exceptions import ContractNotFound, RPCRequestError, decode_typed_error
from brownie.project import build
from brownie.project import main as project_main
from brownie.project.sources import highlight_source
from brownie.test import coverage
from brownie.utils import (
    bright_blue,
    bright_cyan,
    bright_magenta,
    bright_red,
    bright_yellow,
    bytes_to_hexstring,
    color,
    dark_white,
    red,
    hexbytes_to_hexstring,
)
from brownie.utils.output import build_tree

from . import state
from .event import EventDict, _decode_logs, _decode_trace
from .web3 import web3

_T = TypeVar("_T")
_P = ParamSpec("_P")

_marker = deque("-/|\\-/|\\")


def trace_property(fn: Callable[["TransactionReceipt"], _T]) -> "property[_T]":
    # attributes that are only available after querying the tranasaction trace

    @property
    def wrapper(self: "TransactionReceipt") -> _T:
        if self.status < 0:
            return None
        if self._trace_exc is not None:
            raise self._trace_exc
        try:
            return fn(self)
        except RPCRequestError as exc:
            if web3.supports_traces:
                # if the node client supports traces, raise the actual error
                raise exc
            raise RPCRequestError(
                f"Accessing `TransactionReceipt.{fn.__name__}` on a {self.status.name.lower()} "
                "transaction requires the `debug_traceTransaction` RPC endpoint, but the node "
                "client does not support it or has not made it available."
            ) from None

    return wrapper


def trace_inspection(
    fn: Callable[Concatenate["TransactionReceipt", _P], _T],
) -> Callable[Concatenate["TransactionReceipt", _P], _T]:
    def wrapper(self: "TransactionReceipt", *args: _P.args, **kwargs: _P.kwargs) -> _T:
        if self.contract_address:
            raise NotImplementedError(
                "Trace inspection methods are not available for deployment transactions."
            )
        if self.input == "0x" and self.gas_used == 21000:
            return None
        return fn(self, *args, **kwargs)

    functools.update_wrapper(wrapper, fn)
    return wrapper


class Status(IntEnum):
    Dropped = -2
    Pending = -1
    Reverted = 0
    Confirmed = 1


class TransactionReceipt:
    """Attributes and methods relating to a broadcasted transaction.

    * All ether values are given as integers denominated in wei.
    * Before the tx has confirmed, most attributes are set to None
    * Accessing methods / attributes that query debug_traceTransaction
      may be very slow if the transaction involved many steps

    Attributes:
        contract_name: Name of the contract called in the transaction
        fn_name: Name of the method called in the transaction
        txid: Transaction ID
        sender: Address of the sender
        receiver: Address of the receiver
        value: Amount transferred
        gas_price: Gas price
        gas_limit: Gas limit
        gas_used: Gas used
        input: Hexstring input data
        confirmations: The number of blocks since the transaction was confirmed
        nonce: Transaction nonce
        block_number: Block number this transaction was included in
        timestamp: Timestamp of the block this transaction was included in
        txindex: Index of the transaction within the mined block
        contract_address: Address of contract deployed by the transaction
        logs: Raw transaction logs
        status: Transaction status: -1 pending, 0 reverted, 1 successful

    Additional attributes:
    (only available if debug_traceTransaction is enabled in the RPC)

        events: Decoded transaction log events
        trace: Expanded stack trace from debug_traceTransaction
        return_value: Return value(s) from contract call
        revert_msg: Error string from reverted contract all
        modified_state: Boolean, did this contract write to storage?"""

    # these are defined as class attributes to expose them in console completion hints
    block_number = None
    contract_address: Optional[str] = None
    contract_name = None
    fn_name = None
    gas_used = None
    logs: Optional[List] = None
    nonce = None
    sender = None
    txid: str
    txindex = None
    type: int

    def __init__(
        self,
        txid: Union[str, bytes],
        sender: Any = None,
        silent: bool = True,
        required_confs: int = 1,
        is_blocking: bool = True,
        name: str = "",
        revert_data: Optional[Tuple] = None,
    ) -> None:
        """Instantiates a new TransactionReceipt object.

        Args:
            txid: hexstring transaction ID
            sender: sender as a hex string or Account object
            required_confs: the number of required confirmations before processing the receipt
            is_blocking: if True, creating the object is a blocking action until the required
                         confirmations are received
            silent: toggles console verbosity (default True)
            name: contract function being called
            revert_data: (revert string, program counter, revert type)
        """
        self._silent = silent

        if isinstance(txid, bytes):
            txid = bytes_to_hexstring(txid)

        # this event is set once the transaction is confirmed or dropped
        # it is used to waiting during blocking transaction actions
        self._confirmed = threading.Event()

        # internal attributes
        self._call_cost = 0
        self._trace_exc: Optional[Exception] = None
        self._trace_origin: Optional[str] = None
        self._raw_trace: Optional[List] = None
        self._trace: Optional[List] = None
        self._events: Optional[EventDict] = None
        self._return_value: Any = None
        self._revert_msg: Optional[str] = None
        self._dev_revert_msg: Optional[str] = None
        self._modified_state: Optional[bool] = None
        self._new_contracts: Optional[List[EthAddress]] = None
        self._internal_transfers: Optional[List[Dict[str, Any]]] = None
        self._subcalls: Optional[List[Dict[str, Any]]] = None

        # attributes that can be set immediately
        self.sender = sender
        self.status = Status(-1)
        self.txid = str(txid)
        self.contract_name = None
        self.fn_name = name

        if name and "." in name:
            self.contract_name, self.fn_name = name.split(".", maxsplit=1)

        # avoid querying the trace to get the revert string if possible
        self._revert_msg, self._revert_pc, revert_type = revert_data or (None, None, None)
        if self._revert_msg is None and revert_type not in ("revert", "invalid_opcode"):
            self._revert_msg = revert_type
        if self._revert_pc is not None:
            self._dev_revert_msg = build._get_dev_revert(self._revert_pc) or None

        tx: Dict = web3.eth.get_transaction(HexBytes(self.txid))
        self._set_from_tx(tx)

        if not self._silent:
            output_str = ""
            if self.type == 2:
                max_gas = tx["maxFeePerGas"] / 10**9
                priority_gas = tx["maxPriorityFeePerGas"] / 10**9
                output_str = (
                    f"  Max fee: {bright_blue}{max_gas}{color} gwei"
                    f"   Priority fee: {bright_blue}{priority_gas}{color} gwei"
                )
            elif self.gas_price is not None:
                gas_price = self.gas_price / 10**9
                output_str = f"  Gas price: {bright_blue}{gas_price}{color} gwei"
            print(
                f"{output_str}   Gas limit: {bright_blue}{self.gas_limit}{color}"
                f"   Nonce: {bright_blue}{self.nonce}{color}"
            )

        # await confirmation of tx in a separate thread which is blocking if
        # required_confs > 0 or tx has already confirmed (`blockNumber` != None)
        confirm_thread = threading.Thread(
            target=self._await_confirmation,
            args=(tx.get("blockNumber"), required_confs),
            daemon=True,
        )
        confirm_thread.start()
        if is_blocking and (required_confs > 0 or tx.get("blockNumber")):
            confirm_thread.join()

    def __repr__(self) -> str:
        color_str = {-2: "dark white", -1: "bright yellow", 0: "bright red", 1: ""}[self.status]
        return f"<Transaction '{color(color_str)}{self.txid}{color}'>"

    def __hash__(self) -> int:
        return hash(self.txid)

    @trace_property
    def events(self) -> EventDict:
        if self._events is None:
            if self.status:
                # relay contract map so we can decode ds-note logs
                addrs = {log.address for log in self.logs} if self.logs else set()
                contracts = {addr: state._find_contract(addr) for addr in addrs}
                self._events = _decode_logs(self.logs, contracts=contracts)  # type: ignore
            else:
                self._get_trace()
                # get events from the trace - handled lazily so that other
                # trace operations are not blocked in case of a decoding error
                initial_address = str(self.receiver or self.contract_address)
                self._events = _decode_trace(self._raw_trace, initial_address)  # type: ignore
        return self._events

    @trace_property
    def internal_transfers(self) -> List[Dict[str, Any]]:
        if not self.status:
            return []
        if self._internal_transfers is None:
            self._expand_trace()
        return self._internal_transfers

    @trace_property
    def modified_state(self) -> Optional[bool]:
        if not self.status:
            self._modified_state = False
        elif self._modified_state is None:
            self._get_trace()
        return self._modified_state

    @trace_property
    def new_contracts(self) -> List[EthAddress]:
        if not self.status:
            return []
        if self._new_contracts is None:
            self._expand_trace()
        return self._new_contracts

    @trace_property
    def return_value(self) -> Optional[str]:
        if not self.status:
            return None
        if self._return_value is None:
            self._get_trace()
        return self._return_value

    @trace_property
    def revert_msg(self) -> Optional[str]:
        if self.status:
            return None
        if self._revert_msg is None:
            self._get_trace()
        elif self.contract_address and self._revert_msg == "out of gas":
            self._get_trace()
        return self._revert_msg

    @trace_property
    def dev_revert_msg(self) -> Optional[str]:
        if self.status:
            return None
        if self._dev_revert_msg is None:
            self._get_trace()

        return self._dev_revert_msg or None

    @trace_property
    def subcalls(self) -> Optional[List]:
        if self._subcalls is None:
            self._expand_trace()
        subcalls = filter(lambda s: not _is_call_to_precompile(s), self._subcalls)  # type: ignore
        return list(subcalls)

    @trace_property
    def trace(self) -> Optional[List]:
        if self._trace is None:
            self._expand_trace()
        return self._trace

    @property
    def timestamp(self) -> Optional[int]:
        if self.status < 0:
            return None
        return web3.eth.get_block(self.block_number)["timestamp"]

    @property
    def confirmations(self) -> int:
        if not self.block_number:
            return 0
        return web3.eth.block_number - self.block_number + 1

    def replace(
        self,
        increment: Optional[float] = None,
        gas_price: Optional[Wei] = None,
        silent: Optional[bool] = None,
    ) -> "TransactionReceipt":
        """
        Rebroadcast this transaction with a higher gas price.

        Exactly one of `increment` and `gas_price` must be given.

        Arguments
        ---------
        increment : float, optional
            Multiplier applied to the gas price of this transaction in order
            to determine the new gas price. For EIP1559 transactions the multiplier
            is applied to the max_fee, the priority_fee is incremented by 1.1.
        gas_price : Wei, optional
            Absolute gas price to use in the replacement transaction. For EIP1559
            transactions this is the new max_fee, the priority_fee is incremented
            by 1.1.
        silent : bool, optional
            Toggle console verbosity (default is same setting as this transaction)

        Returns
        -------
        TransactionReceipt
            New transaction object
        """
        if increment is None and gas_price is None:
            raise ValueError("Must give one of `increment` or `gas_price`")
        if gas_price is not None and increment is not None:
            raise ValueError("Cannot set `increment` and `gas_price` together")
        if self.status > -1:
            raise ValueError("Transaction has already confirmed")

        if self.gas_price is not None:
            if increment is not None:
                gas_price = Wei(self.gas_price * increment)
            else:
                gas_price = Wei(gas_price)

        max_fee, priority_fee = None, None
        if self.max_fee is not None and self.priority_fee is not None:
            max_fee = gas_price
            priority_fee = Wei(self.priority_fee * 1.1)
            gas_price = None

        if silent is None:
            silent = self._silent

        sender = self.sender
        if isinstance(sender, EthAddress):
            # if the transaction wasn't broadcast during this brownie session,
            # check if the sender is unlocked - we might be able to replace anyway
            from brownie import accounts

            if sender in accounts:
                sender = accounts.at(sender)
            else:
                raise ValueError("Sender address not in `accounts`")

        return sender.transfer(  # type: ignore
            self.receiver,
            self.value,
            gas_limit=self.gas_limit,
            gas_price=gas_price,
            max_fee=max_fee,
            priority_fee=priority_fee,
            data=self.input,
            nonce=self.nonce,
            required_confs=0,
            silent=silent,
        )

    def wait(self, required_confs: int) -> None:
        if required_confs < 1:
            return
        if self.confirmations > required_confs:
            print(f"This transaction already has {self.confirmations} confirmations.")
            return

        if self.nonce is not None:
            # if we know the transaction nonce, it's more efficient to watch the tx count
            # this (i hope) also fixes a longstanding bug that sometimes gave an incorrect
            # "tx dropped without known replacement" error due to a race condition
            while web3.eth.get_transaction_count(str(self.sender)) <= self.nonce:
                time.sleep(1)

        while True:
            try:
                tx: Dict = web3.eth.get_transaction(self.txid)
                break
            except TransactionNotFound:
                if self.nonce is not None:
                    self.status = Status(-2)
                    self._confirmed.set()
                    return
                time.sleep(1)

        self._await_confirmation(tx["blockNumber"], required_confs)

    def _raise_if_reverted(self, exc: Any) -> None:
        if self.status or CONFIG.mode == "console":
            return
        if not web3.supports_traces:
            # if traces are not available, do not attempt to determine the revert reason
            raise exc or ValueError("Execution reverted")

        if self._dev_revert_msg is None:
            # no revert message and unable to check dev string - have to get trace
            self._expand_trace()
        if self.contract_address:
            source = ""
        elif CONFIG.argv["revert"]:
            source = self._traceback_string()
        else:
            source = self._error_string(1)
            contract = state._find_contract(self.receiver)
            if contract:
                marker = "//" if contract._build["language"] == "Solidity" else "#"
                line = self._traceback_string().split("\n")[-1]
                if f"{marker} dev: " in line:
                    self._dev_revert_msg = line[line.index(marker) + len(marker) : -5].strip()

        raise exc._with_attr(
            source=source, revert_msg=self._revert_msg, dev_revert_msg=self._dev_revert_msg
        )

    def _await_confirmation(self, block_number: int = None, required_confs: int = 1) -> None:
        # await first confirmation
        block_number = block_number or self.block_number
        nonce_time = 0.0
        sender_nonce = 0
        while True:
            # every 15 seconds, check if the nonce increased without a confirmation of
            # this specific transaction. if this happens, the tx has likely dropped
            # and we should stop waiting.
            if time.time() - nonce_time > 15:
                sender_nonce = web3.eth.get_transaction_count(str(self.sender))
                nonce_time = time.time()

            try:
                receipt = web3.eth.get_transaction_receipt(HexBytes(self.txid))
            except TransactionNotFound:
                receipt = None
            # the null blockHash check is required for older versions of Parity
            # taken from `web3._utils.transactions.wait_for_transaction_receipt`
            if receipt is not None and receipt["blockHash"] is not None:
                break

            # continuation of the nonce logic 2 sections prior. we must check the receipt
            # after querying the nonce, because in the other order there is a chance that
            # the tx would confirm after checking the receipt but before checking the nonce
            if sender_nonce > self.nonce:  # type: ignore
                self.status = Status(-2)
                self._confirmed.set()
                return

            if not block_number and not self._silent and required_confs > 0:
                if required_confs == 1:
                    sys.stdout.write(f"  Waiting for confirmation... {_marker[0]}\r")
                else:
                    sys.stdout.write(
                        f"  Required confirmations: {bright_yellow}0/"
                        f"{required_confs}{color}   {_marker[0]}\r"
                    )
                _marker.rotate(1)
                sys.stdout.flush()

            time.sleep(1)

        # silence other dropped tx's immediately after confirmation to avoid output weirdness
        for dropped_tx in state.TxHistory().filter(
            sender=self.sender, nonce=self.nonce, key=lambda k: k != self
        ):
            dropped_tx._silent = True

        self.block_number = receipt["blockNumber"]
        # wait for more confirmations if required and handle uncle blocks
        remaining_confs = required_confs
        while remaining_confs > 0 and required_confs > 1:
            try:
                receipt = web3.eth.get_transaction_receipt(self.txid)
                self.block_number = receipt["blockNumber"]
            except TransactionNotFound:
                if not self._silent:
                    sys.stdout.write(f"\r{red}Transaction was lost...{color}{' ' * 8}")
                    sys.stdout.flush()
                # check if tx is still in mempool, this will raise otherwise
                tx = web3.eth.get_transaction(self.txid)
                self.block_number = None
                return self._await_confirmation(tx.get("blockNumber"), required_confs)
            if required_confs - self.confirmations != remaining_confs:
                remaining_confs = required_confs - self.confirmations
                if not self._silent:
                    sys.stdout.write(
                        f"\rRequired confirmations: {bright_yellow}{self.confirmations}/"
                        f"{required_confs}{color}  "
                    )
                    if remaining_confs == 0:
                        sys.stdout.write("\n")
                    sys.stdout.flush()
            if remaining_confs > 0:
                time.sleep(1)

        self._set_from_receipt(receipt)
        # if coverage evaluation is active, evaluate the trace
        if (
            CONFIG.argv["coverage"]
            and not coverage._check_cached(self.coverage_hash)
            and self.trace
        ):
            self._expand_trace()
        if not self._silent and required_confs > 0:
            print(self._confirm_output())

        # set the confirmation event and mark other tx's with the same nonce as dropped
        self._confirmed.set()
        for dropped_tx in state.TxHistory().filter(
            sender=self.sender, nonce=self.nonce, key=lambda k: k != self
        ):
            dropped_tx.status = Status(-2)
            dropped_tx._confirmed.set()

    def _set_from_tx(self, tx: Dict) -> None:
        if not self.sender:
            self.sender = EthAddress(tx["from"])
        self.receiver = EthAddress(tx["to"]) if tx.get("to") else None
        self.value = Wei(tx["value"])
        self.gas_price = tx.get("gasPrice")
        self.max_fee = tx.get("maxFeePerGas")
        self.priority_fee = tx.get("maxPriorityFeePerGas")
        self.gas_limit = tx["gas"]
        self.input = hexbytes_to_hexstring(tx["input"])
        self.nonce = tx["nonce"]
        self.type = int(HexBytes(tx.get("type", 0)).hex(), 16)

        # if receiver is a known contract, set function name
        if self.fn_name:
            return
        try:
            contract = state._find_contract(tx.get("to"))
            if contract is not None:
                self.contract_name = contract._name
                calldata = hexbytes_to_hexstring(tx["input"])
                self.fn_name = contract.get_method(calldata)
        except ContractNotFound:
            # required in case the contract has self destructed
            # other aspects of functionality will be broken, but this way we
            # can at least return a receipt
            pass

    def _set_from_receipt(self, receipt: Dict) -> None:
        """Sets object attributes based on the transaction reciept."""
        self.block_number = receipt["blockNumber"]
        self.txindex = receipt["transactionIndex"]
        self.gas_used = receipt["gasUsed"]
        self.logs = receipt["logs"]
        self.status = Status(receipt["status"])
        if "effectiveGasPrice" in receipt:
            self.gas_price = receipt["effectiveGasPrice"]

        self.contract_address = receipt["contractAddress"]
        if self.contract_address and not self.contract_name:
            self.contract_name = "UnknownContract"

        base = (
            f"{self.nonce}{self.block_number}{self.sender}{self.receiver}"
            f"{self.value}{self.input}{int(self.status)}{self.gas_used}{self.txindex}"
        )
        self.coverage_hash = sha1(base.encode()).hexdigest()

        if self.fn_name:
            state.TxHistory()._gas(self._full_name(), receipt["gasUsed"], self.status == Status(1))

    def _confirm_output(self) -> str:
        status = ""
        if not self.status:
            revert_msg = self.revert_msg if web3.supports_traces else None
            status = f"({bright_red}{revert_msg or 'reverted'}{color}) "
        result = (
            f"\r  {self._full_name()} confirmed {status}  "
            f"Block: {bright_blue}{self.block_number}{color}   "
            f"Gas used: {bright_blue}{self.gas_used}{color} "
            f"({bright_blue}{self.gas_used / self.gas_limit:.2%}{color})"
        )
        if self.type == 2 and self.gas_price is not None:
            result += f"   Gas price: {bright_blue}{self.gas_price / 10 ** 9}{color} gwei"
        if self.status and self.contract_address:
            result += (
                f"\n  {self.contract_name} deployed at: "
                f"{bright_blue}{self.contract_address}{color}"
            )
        return result + "\n"

    def _get_trace(self) -> None:
        """Retrieves the stack trace via debug_traceTransaction and finds the
        return value, revert message and event logs in the trace.
        """

        # check if trace has already been retrieved, or the tx warrants it
        if self._raw_trace is not None:
            return
        self._raw_trace = []
        if self.input == "0x" and self.gas_used == 21000:
            self._modified_state = False
            self._trace = []
            return

        if not web3.supports_traces:
            raise RPCRequestError("Node client does not support `debug_traceTransaction`")
        try:
            trace = web3.provider.make_request(  # type: ignore
                # Set enableMemory to all RPC as anvil return the memory key
                "debug_traceTransaction",
                (self.txid, {"disableStorage": CONFIG.mode != "console", "enableMemory": True}),
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            msg = f"Encountered a {type(e).__name__} while requesting "
            msg += "`debug_traceTransaction`. The local RPC client has likely crashed."
            if CONFIG.argv["coverage"]:
                msg += " If the error persists, add the `skip_coverage` marker to this test."
            raise RPCRequestError(msg) from None

        if "error" in trace:
            self._modified_state = None
            self._trace_exc = RPCRequestError(trace["error"]["message"])
            raise self._trace_exc

        self._raw_trace = trace = trace["result"]["structLogs"]
        if not trace:
            self._modified_state = False
            return

        # different nodes return slightly different formats. its really fun to handle
        # geth/nethermind returns unprefixed and with 0-padding for stack and memory
        # erigon returns 0x-prefixed and without padding (but their memory values are like geth)
        fix_stack = False
        for step in trace:
            if not step["stack"]:
                continue
            check = step["stack"][0]
            if not isinstance(check, str):
                break
            if check.startswith("0x"):
                fix_stack = True
                break

        fix_gas = isinstance(trace[0]["gas"], str)

        if fix_stack or fix_gas:
            for step in trace:
                if fix_stack:
                    # for stack values, we need 32 bytes (64 chars) without the 0x prefix
                    # NOTE removeprefix is used for compatability with both hexbytes<1 and >=1
                    step["stack"] = [
                        HexBytes(s).hex().removeprefix("0x").zfill(64) for s in step["stack"]
                    ]
                if fix_gas:
                    # handle traces where numeric values are returned as hex (Nethermind)
                    step["gas"] = int(step["gas"], 16)
                    # Check if gasCost is  hex before converting.
                    if isinstance(step["gasCost"], str):
                        step["gasCost"] = int.from_bytes(
                            HexBytes(step["gasCost"]), "big", signed=True
                        )
                    if isinstance(step["pc"], str):  # Check if pc is hex before converting.
                        step["pc"] = int(step["pc"], 16)

        if self.status:
            self._confirmed_trace(trace)
        else:
            self._reverted_trace(trace)

    def _confirmed_trace(self, trace: Sequence) -> None:
        self._modified_state = next((True for i in trace if i["op"] == "SSTORE"), False)

        if trace[-1]["op"] != "RETURN" or self.contract_address:
            return
        contract = state._find_contract(self.receiver)
        if contract:
            data = _get_memory(trace[-1], -1)
            fn = contract.get_method_object(self.input)
            if not fn:
                warn(f"Unable to find function on {contract} for input {self.input}")
                return
            self._return_value = fn.decode_output(data)

    def _reverted_trace(self, trace: Sequence) -> None:
        self._modified_state = False
        if self.contract_address:
            step = next((i for i in trace if i["op"] == "CODECOPY"), None)
            if step is not None and int(step["stack"][-3], 16) > 24577:
                self._revert_msg = "exceeds EIP-170 size limit"
                self._dev_revert_msg = ""

        if self._dev_revert_msg is not None:
            return

        # iterate over revert instructions in reverse to find revert message
        for step in (i for i in trace[::-1] if i["op"] in ("REVERT", "INVALID")):
            if step["op"] == "REVERT" and int(step["stack"][-2], 16):
                # get returned error string from stack
                data = _get_memory(step, -1)
                self._revert_msg = decode_typed_error(hexbytes_to_hexstring(data))
            elif self.contract_address:
                self._revert_msg = "invalid opcode" if step["op"] == "INVALID" else ""
                self._dev_revert_msg = ""
                return

            # check for dev revert string using program counter
            dev_revert = build._get_dev_revert(step["pc"]) or None
            if dev_revert is not None:
                self._dev_revert_msg = dev_revert
                if self._revert_msg is None:
                    self._revert_msg = dev_revert
            else:
                # if none is found, expand the trace and get it from the pcMap
                self._expand_trace()
                try:
                    contract = state._find_contract(step["address"])
                    pc_map = contract._build["pcMap"]
                    # if this is the function selector revert, check for a jump
                    if "first_revert" in pc_map[step["pc"]]:
                        idx = trace.index(step) - 4
                        if trace[idx]["pc"] != step["pc"] - 4:
                            step = trace[idx]

                    # if this is the optimizer revert, find the actual source
                    if "optimizer_revert" in pc_map[step["pc"]]:
                        idx = trace.index(step) - 1

                        # look for the most recent jump
                        while trace[idx + 1]["op"] != "JUMPDEST":
                            if trace[idx]["source"] != step["source"]:
                                # if we find another line with a differing source offset prior
                                # to a JUMPDEST, the optimizer revert is also the actual revert
                                idx = trace.index(step)
                                break
                            idx -= 1
                        while not trace[idx]["source"]:
                            # now we're in a yul optimization, keep stepping back
                            # until we find a source offset
                            idx -= 1
                        # at last we have the real location of the revert
                        step["source"] = trace[idx]["source"]
                        step = trace[idx]

                    if "dev" in pc_map[step["pc"]]:
                        self._dev_revert_msg = pc_map[step["pc"]]["dev"]
                    else:
                        # extract the dev revert string from the source code
                        # TODO this technique appears superior to `_get_dev_revert`, and
                        # changes in solc 0.8.0 have necessitated it. the old approach
                        # of building a dev revert map should be refactored out in favor
                        # of this one.
                        source = contract._sources.get(step["source"]["filename"])
                        offset = step["source"]["offset"][1]
                        line = source[offset:].split("\n")[0]
                        marker = "//" if contract._build["language"] == "Solidity" else "#"
                        revert_str = line[line.index(marker) + len(marker) :].strip()
                        if revert_str.startswith("dev:"):
                            self._dev_revert_msg = revert_str

                    if self._revert_msg is None:
                        self._revert_msg = self._dev_revert_msg or ""
                    return
                except (KeyError, AttributeError, TypeError, ValueError):
                    pass

            if self._revert_msg is not None:
                if self._dev_revert_msg is None:
                    self._dev_revert_msg = ""
                return

        op = next((i["op"] for i in trace[::-1] if i["op"] in ("REVERT", "INVALID")), None)
        self._revert_msg = "invalid opcode" if op == "INVALID" else ""

    def _expand_trace(self) -> None:
        """Adds the following attributes to each step of the stack trace:

        address: The address executing this contract.
        contractName: The name of the contract.
        fn: The name of the function.
        jumpDepth: Number of jumps made since entering this contract. The
                   initial value is 0.
        source: {
            filename: path to the source file for this step
            offset: Start and end offset associated source code
        }
        """
        if self._raw_trace is None:
            self._get_trace()
        if self._trace is not None:
            # in case `_get_trace` also expanded the trace, do not repeat
            return

        self._trace = trace = self._raw_trace
        self._new_contracts = []
        self._internal_transfers = []
        self._subcalls = []
        if self.contract_address or not trace:
            coverage._add_transaction(self.coverage_hash, {})
            return

        if trace[0]["depth"] == 1:
            self._trace_origin = "geth"
            self._call_cost = self.gas_used - trace[0]["gas"] + trace[-1]["gas"]
            for t in trace:
                t["depth"] = t["depth"] - 1
        else:
            self._trace_origin = "ganache"
            if trace[0]["gasCost"] >= 21000:
                # in ganache <6.10.0, gas costs are shifted by one step - we can
                # identify this when the first step has a gas cost >= 21000
                self._call_cost = trace[0]["gasCost"]
                for i in range(len(trace) - 1):
                    trace[i]["gasCost"] = trace[i + 1]["gasCost"]
                trace[-1]["gasCost"] = 0
            else:
                self._call_cost = self.gas_used - trace[0]["gas"] + trace[-1]["gas"]

        # last_map gives a quick reference of previous values at each depth
        last_map = {0: _get_last_map(self.receiver, self.input[:10])}  # type: ignore
        coverage_eval: Dict = {last_map[0]["name"]: {}}
        precompile_contract = regex_compile(r"0x0{38}(?:0[1-9]|1[0-8])")
        call_opcodes = ("CALL", "STATICCALL", "DELEGATECALL")
        for i in range(len(trace)):
            # if depth has increased, tx has called into a different contract
            is_depth_increase = trace[i]["depth"] > trace[i - 1]["depth"]
            is_subcall = trace[i - 1]["op"] in call_opcodes
            if is_depth_increase or is_subcall:
                step = trace[i - 1]
                if step["op"] in ("CREATE", "CREATE2"):
                    # creating a new contract
                    out = next(x for x in trace[i:] if x["depth"] == step["depth"])
                    address = out["stack"][-1][-40:]
                    sig = f"<{step['op']}>"
                    calldata = None
                    self._new_contracts.append(EthAddress(address))
                    if int(step["stack"][-1], 16):
                        self._add_internal_xfer(step["address"], address, step["stack"][-1])
                else:
                    # calling an existing contract
                    stack_idx = -4 if step["op"] in ("CALL", "CALLCODE") else -3
                    offset = int(step["stack"][stack_idx], 16)
                    length = int(step["stack"][stack_idx - 1], 16)
                    calldata = HexBytes("".join(step["memory"]))[offset : offset + length]
                    sig = hexbytes_to_hexstring(calldata[:4])
                    address = step["stack"][-2][-40:]

                if is_depth_increase:
                    last_map[trace[i]["depth"]] = _get_last_map(address, sig)
                    coverage_eval.setdefault(last_map[trace[i]["depth"]]["name"], {})

                self._subcalls.append(
                    {"from": step["address"], "to": EthAddress(address), "op": step["op"]}
                )
                if step["op"] in ("CALL", "CALLCODE"):
                    self._subcalls[-1]["value"] = int(step["stack"][-3], 16)
                if is_depth_increase and calldata and last_map[trace[i]["depth"]].get("function"):
                    fn = last_map[trace[i]["depth"]]["function"]
                    self._subcalls[-1]["function"] = fn._input_sig
                    try:
                        zip_ = zip(fn.abi["inputs"], fn.decode_input(calldata))
                        inputs = {i[0]["name"]: i[1] for i in zip_}  # type: ignore
                        self._subcalls[-1]["inputs"] = inputs
                    except Exception:
                        self._subcalls[-1]["calldata"] = hexbytes_to_hexstring(calldata)
                elif calldata or is_subcall:
                    self._subcalls[-1]["calldata"] = hexbytes_to_hexstring(calldata)

                if precompile_contract.search(str(self._subcalls[-1]["from"])) is not None:
                    caller = self._subcalls.pop(-2)["from"]
                    self._subcalls[-1]["from"] = caller

            # update trace from last_map
            last = last_map[trace[i]["depth"]]
            trace[i].update(
                address=last["address"],
                contractName=last["name"],
                fn=last["internal_calls"][-1],
                jumpDepth=last["jumpDepth"],
                source=False,
            )

            opcode = trace[i]["op"]
            if opcode == "CALL" and int(trace[i]["stack"][-3], 16):
                self._add_internal_xfer(
                    last["address"], trace[i]["stack"][-2][-40:], trace[i]["stack"][-3]
                )

            # If the function signature is not available for decoding return data attach
            # the encoded data.
            # If the function signature is available this will be overridden by setting
            # `return_value` a few lines below.
            if trace[i]["depth"] and opcode == "RETURN":
                subcall: dict = next(
                    i for i in self._subcalls[::-1] if i["to"] == last["address"]  # type: ignore
                )

                if opcode == "RETURN":
                    returndata = _get_memory(trace[i], -1)
                    if returndata.hex().removeprefix("0x"):
                        subcall["returndata"] = hexbytes_to_hexstring(returndata)

            try:
                pc = last["pc_map"][trace[i]["pc"]]
            except (KeyError, TypeError):
                # we don't have enough information about this contract
                continue

            if trace[i]["depth"] and opcode in ("RETURN", "REVERT", "INVALID", "SELFDESTRUCT"):
                subcall: dict = next(
                    i for i in self._subcalls[::-1] if i["to"] == last["address"]  # type: ignore
                )

                if opcode == "RETURN":
                    returndata = _get_memory(trace[i], -1)
                    if returndata:
                        fn = last["function"]
                        try:
                            return_values = fn.decode_output(returndata)
                            if len(fn.abi["outputs"]) == 1:
                                return_values = (return_values,)
                            subcall["return_value"] = return_values
                        except Exception:
                            subcall["returndata"] = hexbytes_to_hexstring(returndata)
                    else:
                        subcall["return_value"] = None
                elif opcode == "SELFDESTRUCT":
                    subcall["selfdestruct"] = True
                else:
                    if opcode == "REVERT":
                        data = _get_memory(trace[i], -1)
                        if len(data) > 4:
                            try:
                                subcall["revert_msg"] = decode(["string"], data[4:])[0]
                            except Exception:
                                subcall["revert_msg"] = hexbytes_to_hexstring(data)
                    if "revert_msg" not in subcall and "dev" in pc:
                        subcall["revert_msg"] = pc["dev"]

            if "path" not in pc:
                continue
            trace[i]["source"] = {"filename": last["path_map"][pc["path"]], "offset": pc["offset"]}

            if "fn" not in pc:
                continue

            # calculate coverage
            if last["coverage"]:
                if pc["path"] not in coverage_eval[last["name"]]:
                    coverage_eval[last["name"]][pc["path"]] = [set(), set(), set()]
                if "statement" in pc:
                    coverage_eval[last["name"]][pc["path"]][0].add(pc["statement"])
                if "branch" in pc:
                    if pc["op"] != "JUMPI":
                        last["active_branches"].add(pc["branch"])
                    elif "active_branches" not in last or pc["branch"] in last["active_branches"]:
                        # false, true
                        key = 1 if trace[i + 1]["pc"] == trace[i]["pc"] + 1 else 2
                        coverage_eval[last["name"]][pc["path"]][key].add(pc["branch"])
                        if "active_branches" in last:
                            last["active_branches"].remove(pc["branch"])

            # ignore jumps with no function - they are compiler optimizations
            if "jump" in pc:
                # jump 'i' is calling into an internal function
                if pc["jump"] == "i":
                    try:
                        fn = last["pc_map"][trace[i + 1]["pc"]]["fn"]
                    except (KeyError, IndexError):
                        continue
                    if fn != last["internal_calls"][-1]:
                        last["internal_calls"].append(fn)
                        last["jumpDepth"] += 1
                # jump 'o' is returning from an internal function
                elif last["jumpDepth"] > 0:
                    del last["internal_calls"][-1]
                    last["jumpDepth"] -= 1
        coverage._add_transaction(
            self.coverage_hash, dict((k, v) for k, v in coverage_eval.items() if v)
        )

    def _add_internal_xfer(self, from_: str, to: str, value: str) -> None:
        if not value.startswith("0x"):
            value = f"0x{value}"

        self._internal_transfers.append(  # type: ignore
            {"from": EthAddress(from_), "to": EthAddress(to), "value": Wei(value)}
        )

    def _full_name(self) -> str:
        if self.contract_name and self.fn_name:
            return f"{self.contract_name}.{self.fn_name}"
        return self.fn_name or "Transaction"

    def info(self) -> None:
        """Displays verbose information about the transaction, including decoded event logs."""
        result = f"Tx Hash: {self.txid}\nFrom: {self.sender}\n"
        if self.contract_address and self.status:
            result += f"New {self.contract_name} address: {self.contract_address}\n"
        else:
            result += f"To: {self.receiver}\n" f"Value: {self.value}\n"
            if self.input != "0x" and int(self.input, 16):
                result += f"Function: {self._full_name()}\n"

        result += (
            f"Block: {self.block_number}\nGas Used: "
            f"{self.gas_used} / {self.gas_limit} "
            f"({self.gas_used / self.gas_limit:.1%})\n"
        )

        if self.events:
            events = list(self.events)
            call_tree: List = ["--------------------------"]
            while events:
                idx = next(
                    (events.index(i) for i in events if i.address != events[0].address), len(events)
                )
                contract = state._find_contract(events[0].address)
                if contract:
                    try:
                        name = contract.name()
                    except Exception:
                        name = contract._name
                    sub_tree: List = [f"{name} ({events[0].address})"]
                else:
                    sub_tree = [f"{events[0].address}"]
                for event in events[:idx]:
                    sub_tree.append([event.name, *(f"{k}: {v}" for k, v in event.items())])
                call_tree.append(sub_tree)
                events = events[idx:]
            event_tree = build_tree([call_tree], multiline_pad=0, pad_depth=[0, 1])
            result = f"{result}\nEvents In This Transaction\n{event_tree}"

        result = color.highlight(result)
        status = ""
        if not self.status:
            status = f"({bright_red}{self.revert_msg or 'reverted'}{color})"
        print(f"Transaction was Mined {status}\n---------------------\n{result}")

    def _get_trace_gas(self, start: int, stop: int) -> Tuple[int, int]:
        total_gas = 0
        internal_gas = 0
        is_internal = True
        trace = self.trace

        for i in range(start, stop):
            # Check if we are in a subfunction or not
            if is_internal and not _step_compare(trace[i], trace[start]):
                is_internal = False
                # For the internal gas tracking we ignore the gas passed to an external call
                if trace[i]["depth"] > trace[start]["depth"]:
                    internal_gas -= trace[i - 1]["gasCost"]
            elif not is_internal and _step_compare(trace[i], trace[start]):
                is_internal = True

            total_gas += trace[i]["gasCost"]
            if is_internal:
                internal_gas += trace[i]["gasCost"]

            # manually add gas refunds where they occur
            if trace[i]["op"] == "SSTORE" and int(trace[i]["stack"][-2], 16) == 0:
                # 15000 gas is refunded if a word is set to 0x0
                # Note: There is currently no way to check if the value was 0x0 before.
                # This will give an incorrect refund if 0x0 is assigned to 0x0.
                total_gas -= 15000
                if is_internal:
                    internal_gas -= 15000
            if trace[i]["op"] == "SELFDESTRUCT":
                # 24000 gas is refunded on selfdestruct
                total_gas -= 24000
                if is_internal:
                    internal_gas -= 24000

        # For external calls, add the remaining gas returned back
        if start > 0 and trace[start]["depth"] > trace[start - 1]["depth"]:
            total_gas += trace[start - 1]["gasCost"]
            internal_gas += trace[start - 1]["gasCost"]

        return internal_gas, total_gas

    @trace_inspection
    def call_trace(self, expand: bool = False) -> None:
        """
        Display the complete sequence of contracts and methods called during
        the transaction. The format:

        Contract.functionName  [instruction]  start:stop  [gas used]

        * start:stop are index values for the `trace` member of this object,
          showing the points where the call begins and ends
        * for calls that include subcalls, gas use is displayed as
          [gas used in this frame / gas used in this frame + subcalls]
        * Calls displayed in red ended with a `REVERT` or `INVALID` instruction.

        Arguments
        ---------
        expand : bool
            If `True`, show an expanded call trace including inputs and return values
        """

        trace = self.trace
        key = _step_internal(
            trace[0], trace[-1], 0, len(trace), self._get_trace_gas(0, len(self.trace))
        )

        call_tree: List = [[key]]
        active_tree: List = [call_tree[0]]

        # (index, depth, jumpDepth) for relevent steps in the trace
        trace_index = [(0, 0, 0)] + [
            (i, trace[i]["depth"], trace[i]["jumpDepth"])
            for i in range(1, len(trace))
            if not _step_compare(trace[i], trace[i - 1])
        ]

        subcalls = self.subcalls[::-1]
        for i, (idx, depth, jump_depth) in enumerate(trace_index[1:], start=1):
            last = trace_index[i - 1]
            if depth == last[1] and jump_depth < last[2]:
                # returning from an internal function, reduce tree by one
                active_tree.pop()
                continue
            elif depth < last[1]:
                # returning from an external call, return tree by jumpDepth of the previous depth
                active_tree = active_tree[: -(last[2] + 1)]
                continue

            if depth > last[1]:
                # called to a new contract
                end = next((x[0] for x in trace_index[i + 1 :] if x[1] < depth), len(trace))
                total_gas, internal_gas = self._get_trace_gas(idx, end)
                key = _step_external(
                    trace[idx],
                    trace[end - 1],
                    idx,
                    end,
                    (total_gas, internal_gas),
                    subcalls.pop(),
                    expand,
                )
            elif depth == last[1] and jump_depth > last[2]:
                # jumped into an internal function
                end = next(
                    (
                        x[0]
                        for x in trace_index[i + 1 :]
                        if x[1] < depth or (x[1] == depth and x[2] < jump_depth)
                    ),
                    len(trace),
                )

                total_gas, internal_gas = self._get_trace_gas(idx, end)
                key = _step_internal(
                    trace[idx], trace[end - 1], idx, end, (total_gas, internal_gas)
                )

            active_tree[-1].append([key])
            active_tree.append(active_tree[-1][-1])

        print(
            f"Call trace for '{bright_blue}{self.txid}{color}':\n"
            f"Initial call cost  [{bright_yellow}{self._call_cost} gas{color}]"
        )
        print(build_tree(call_tree).rstrip())

    def traceback(self) -> None:
        print(self._traceback_string() or "")

    @trace_inspection
    def _traceback_string(self) -> str:
        """Returns an error traceback for the transaction."""
        if self.status == 1:
            return ""
        trace = self.trace

        try:
            idx = next(i for i in range(len(trace)) if trace[i]["op"] in ("REVERT", "INVALID"))
            trace_range = range(idx, -1, -1)
        except StopIteration:
            return ""

        try:
            result = [next(i for i in trace_range if trace[i]["source"])]
        except StopIteration:
            return ""
        depth, jump_depth = trace[idx]["depth"], trace[idx]["jumpDepth"]

        while True:
            try:
                idx = next(
                    i
                    for i in trace_range
                    if trace[i]["depth"] < depth
                    or (trace[i]["depth"] == depth and trace[i]["jumpDepth"] < jump_depth)
                )
                result.append(idx)
                depth, jump_depth = trace[idx]["depth"], trace[idx]["jumpDepth"]
            except StopIteration:
                break
        return f"{color}Traceback for '{bright_blue}{self.txid}{color}':\n" + "\n".join(
            self._source_string(i, 0) for i in result[::-1]
        )

    def error(self, pad: int = 3) -> None:
        print(self._error_string(pad) or "")

    @trace_inspection
    def _error_string(self, pad: int = 3) -> str:
        """Returns the source code that caused the transaction to revert.

        Args:
            pad: Number of unrelated lines of code to include before and after

        Returns: source code string
        """
        if self.status == 1:
            return ""

        # if RPC returned a program counter, try to find source without querying trace
        if self._revert_pc:
            highlight, linenos, path, fn_name = build._get_error_source_from_pc(self._revert_pc)
            if highlight:
                return _format_source(highlight, linenos, path, self._revert_pc, -1, fn_name)
            self._revert_pc = None

        # iterate backward through the trace until a step has a source offset
        trace = self.trace
        trace_range = range(len(trace) - 1, -1, -1)
        try:
            idx = next(i for i in trace_range if trace[i]["op"] in {"REVERT", "INVALID"})
            idx = next(i for i in trace_range if trace[i]["source"])
            return self._source_string(idx, pad)
        except StopIteration:
            return ""

    def source(self, idx: int, pad: int = 3) -> None:
        print(self._source_string(idx, pad) or "")

    @trace_inspection
    def _source_string(self, idx: int, pad: int) -> str:
        """Displays the associated source code for a given stack trace step.

        Args:
            idx: Stack trace step index
            pad: Number of unrelated lines of code to include before and after

        Returns: source code string
        """
        trace = self.trace[idx]
        if not trace.get("source", None):
            return ""
        contract = state._find_contract(self.trace[idx]["address"])
        source, linenos = highlight_source(
            contract._sources.get(trace["source"]["filename"]), trace["source"]["offset"], pad
        )
        if not source:
            return ""
        return _format_source(
            source,
            linenos,
            trace["source"]["filename"],
            trace["pc"],
            self.trace.index(trace),
            trace["fn"],
        )


def _format_source(source: str, linenos: Tuple, path: Path, pc: int, idx: int, fn_name: str) -> str:
    ln = f" {bright_blue}{linenos[0]}"
    if linenos[1] > linenos[0]:
        ln = f"s{ln}{dark_white}-{bright_blue}{linenos[1]}"
    return (
        f"{dark_white}Trace step {bright_blue}{idx}{dark_white}, "
        f"program counter {bright_blue}{pc}{dark_white}:\n  {dark_white}"
        f'File {bright_magenta}"{path}"{dark_white}, line{ln}'
        f"{dark_white}, in {bright_cyan}{fn_name}{dark_white}:{source}"
    )


def _step_compare(a: Dict, b: Dict) -> bool:
    return a["depth"] == b["depth"] and a["jumpDepth"] == b["jumpDepth"]


def _step_internal(
    step: Dict,
    last_step: Dict,
    start: Union[str, int],
    stop: Union[str, int],
    gas: Tuple[int, int],
    subcall: Dict = None,
) -> str:
    if last_step["op"] in {"REVERT", "INVALID"} and _step_compare(step, last_step):
        contract_color = color("bright red")
    else:
        contract_color = color() if step["jumpDepth"] else color("bright cyan")
    key = f"{dark_white}{contract_color}{step['fn']}  {dark_white}"

    left_bracket = f"{dark_white}["
    right_bracket = f"{dark_white}]"

    if subcall:
        key = f"{key}[{color}{subcall['op']}{right_bracket}  "

    key = f"{key}{start}:{stop}{color}"

    if gas:
        if gas[0] == gas[1]:
            gas_str = f"{bright_yellow}{gas[0]} gas"
        else:
            gas_str = f"{bright_yellow}{gas[0]} / {gas[1]} gas"
        key = f"{key}  {left_bracket}{gas_str}{right_bracket}{color}"

    if last_step["op"] == "SELFDESTRUCT":
        key = f"{key}  {left_bracket}{bright_red}SELFDESTRUCT{right_bracket}{color}"

    return key


def _convert_0x_to_empty_bytes(value: Any) -> Any:
    # black cannot parse `0x` without any trailing zeros, so we temporarily
    # replace it with an empty bytestring
    final = []
    for item in value:
        if isinstance(item, (list, tuple)):
            final.append(_convert_0x_to_empty_bytes(item))
        elif str(item) == "0x":
            final.append(b"")
        else:
            final.append(item)
    return type(value)(final)


def _format(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = _convert_0x_to_empty_bytes(value)
        mode = black.FileMode(line_length=60)
        value = black.format_str(str(value), mode=mode).replace('b""', "0x")
    return str(value)


def _step_external(
    step: Dict,
    last_step: Dict,
    start: Union[str, int],
    stop: Union[str, int],
    gas: Tuple[int, int],
    subcall: Dict,
    expand: bool,
) -> str:
    key = _step_internal(step, last_step, start, stop, gas, subcall)
    if not expand:
        return key

    result: List = [key, f"address: {step['address']}"]

    if "value" in subcall:
        result.append(f"value: {subcall['value']}")

    if "inputs" not in subcall:
        result.append(f"calldata: {subcall.get('calldata')}")
    elif subcall["inputs"]:
        result.append(
            ["input arguments:", *(f"{k}: {_format(v)}" for k, v in subcall["inputs"].items())]
        )
    else:
        result.append("input arguments: None")

    if "return_value" in subcall:
        value = subcall["return_value"]
        if isinstance(value, tuple) and len(value) > 1:
            result.append(["return values:", *(_format(i) for i in value)])
        else:
            if isinstance(value, tuple):
                value = value[0]
            result.append(f"return value: {_format(value)}")
    elif "returndata" in subcall:
        result.append(f"returndata: {subcall['returndata']}")

    if "revert_msg" in subcall:
        result.append(f"revert reason: {bright_red}{subcall['revert_msg']}{color}")

    return build_tree([result], multiline_pad=0).rstrip()


def _get_memory(step: Dict, idx: int) -> HexBytes:
    offset = int(step["stack"][idx], 16)
    length = int(step["stack"][idx - 1], 16)
    data = HexBytes("".join(step["memory"]))[offset : offset + length]
    # append zero-bytes if allocated memory ends before `length` bytes
    data = HexBytes(data + b"\x00" * (length - len(data)))
    return data


def _get_last_map(address: EthAddress, sig: str) -> Dict:
    contract = state._find_contract(address)
    last_map = {"address": EthAddress(address), "jumpDepth": 0, "name": None, "coverage": False}

    if contract:
        if contract.get_method(sig):
            full_fn_name = f"{contract._name}.{contract.get_method(sig)}"
        else:
            full_fn_name = contract._name
        last_map.update(
            contract=contract,
            function=contract.get_method_object(sig),
            name=contract._name,
            internal_calls=[full_fn_name],
            path_map=contract._build.get("allSourcePaths"),
            pc_map=contract._build.get("pcMap"),
        )
        if isinstance(contract._project, project_main.Project):
            # only evaluate coverage for contracts that are part of a `Project`
            last_map["coverage"] = True
            if contract._build.get("language") == "Solidity":
                last_map["active_branches"] = set()
    else:
        last_map.update(contract=None, internal_calls=[f"<UnknownContract>.{sig}"], pc_map=None)

    return last_map


def _is_call_to_precompile(subcall: dict) -> bool:
    precompile_contract = regex_compile(r"0x0{38}(?:0[1-9]|1[0-8])")
    return True if precompile_contract.search(str(subcall["to"])) is not None else False
