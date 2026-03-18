import asyncio
import re
from typing import Union, Any

from .. import cdp
from .connection import Connection


class BaseRequestExpectation:
    """
    Base class for handling request and response expectations.
    This class provides a context manager to wait for specific network requests and responses
    based on a URL pattern. It sets up handlers for request and response events and provides
    properties to access the request, response, and response body.
    :param tab: The Tab instance to monitor.
    :type tab: Tab
    :param url_pattern: The URL pattern(s) to match requests and responses. Can be a single pattern or a list of patterns.
                        When multiple patterns are provided with count parameter, waits until ALL patterns have matched at least once.
    :type url_pattern: Union[str, re.Pattern[str], list[Union[str, re.Pattern[str]]]]
    :param count: Enable multiple expectation mode. When set with multiple patterns, waits until all patterns have matched.
                  If None, waits for one match (single expectation mode).
    :type count: Union[int, None]
    """

    def __init__(self, tab: Connection, url_pattern: Union[str, re.Pattern[str], list[Union[str, re.Pattern[str]]]], count: Union[int, None] = None):
        self.tab = tab
        # Normalize to list of patterns
        if isinstance(url_pattern, list):
            self.url_patterns = url_pattern
        else:
            self.url_patterns = [url_pattern]
        self.count = count
        self.request_future: asyncio.Future[cdp.network.RequestWillBeSent] = (
            asyncio.Future()
        )
        self.response_future: asyncio.Future[cdp.network.ResponseReceived] = (
            asyncio.Future()
        )
        self.loading_finished_future: asyncio.Future[cdp.network.LoadingFinished] = (
            asyncio.Future()
        )
        self.request_id: Union[cdp.network.RequestId, None] = None

        # For multiple expectations
        self.requests: list[cdp.network.RequestWillBeSent] = []
        self.responses: list[cdp.network.ResponseReceived] = []
        self.loading_finished_events: list[cdp.network.LoadingFinished] = []
        self.request_ids: list[cdp.network.RequestId] = []
        self.all_done_future: Union[asyncio.Future[None], None] = None if count is None else asyncio.Future()

        # Track which patterns have been matched
        self.matched_patterns: set[int] = set()  # Indices of matched patterns
        self.request_id_to_pattern: dict[cdp.network.RequestId, int] = {}  # Map request_id to pattern index
        self.matched_patterns_responses: set[int] = set()  # Patterns that have received responses
        self.matched_patterns_loading: set[int] = set()  # Patterns that have finished loading

    def _matches_any_pattern(self, url: str) -> Union[int, None]:
        """
        Check if URL matches any of the configured patterns.
        :param url: The URL to check.
        :type url: str
        :return: Index of the first matching pattern, or None if no match.
        :rtype: Union[int, None]
        """
        for idx, pattern in enumerate(self.url_patterns):
            if re.fullmatch(pattern, url):
                return idx
        return None

    def _all_patterns_matched(self) -> bool:
        """
        Check if all patterns have been matched at least once.
        :return: True if all patterns matched.
        :rtype: bool
        """
        return len(self.matched_patterns) == len(self.url_patterns)

    async def _request_handler(self, event: cdp.network.RequestWillBeSent) -> None:
        """
        Internal handler for request events.
        :param event: The request event.
        """
        pattern_idx = self._matches_any_pattern(event.request.url)
        if pattern_idx is not None:
            if self.count is None:
                # Single expectation mode
                self._remove_request_handler()
                self.request_id = event.request_id
                # ensure the future is not already done
                if not self.request_future.done():
                    self.request_future.set_result(event)
            else:
                # Multiple expectation mode
                self.requests.append(event)
                self.request_ids.append(event.request_id)
                self.request_id_to_pattern[event.request_id] = pattern_idx
                self.matched_patterns.add(pattern_idx)
                if self._all_patterns_matched():
                    self._remove_request_handler()

    async def _response_handler(self, event: cdp.network.ResponseReceived) -> None:
        """
        Internal handler for response events.
        :param event: The response event.
        """
        if self.count is None:
            # Single expectation mode
            if event.request_id == self.request_id:
                self._remove_response_handler()
                self.response_future.set_result(event)
        else:
            # Multiple expectation mode
            if event.request_id in self.request_ids:
                self.responses.append(event)
                # Track which pattern received a response
                pattern_idx = self.request_id_to_pattern.get(event.request_id)
                if pattern_idx is not None:
                    self.matched_patterns_responses.add(pattern_idx)
                # Remove handler once all patterns have received responses
                if len(self.matched_patterns_responses) >= len(self.url_patterns):
                    self._remove_response_handler()

    async def _loading_finished_handler(
        self, event: cdp.network.LoadingFinished
    ) -> None:
        """
        Internal handler for loading finished events.
        :param event: The loading finished event.
        """
        if self.count is None:
            # Single expectation mode
            if event.request_id == self.request_id:
                self._remove_loading_finished_handler()
                self.loading_finished_future.set_result(event)
        else:
            # Multiple expectation mode
            if event.request_id in self.request_ids:
                self.loading_finished_events.append(event)
                # Track which pattern finished loading
                pattern_idx = self.request_id_to_pattern.get(event.request_id)
                if pattern_idx is not None:
                    self.matched_patterns_loading.add(pattern_idx)
                # Complete once all patterns have finished loading
                if len(self.matched_patterns_loading) >= len(self.url_patterns):
                    self._remove_loading_finished_handler()
                    if self.all_done_future and not self.all_done_future.done():
                        self.all_done_future.set_result(None)

    def _remove_request_handler(self) -> None:
        """
        Remove the request event handler.
        """
        self.tab.remove_handlers(cdp.network.RequestWillBeSent, self._request_handler)

    def _remove_response_handler(self) -> None:
        """
        Remove the response event handler.
        """
        self.tab.remove_handlers(cdp.network.ResponseReceived, self._response_handler)

    def _remove_loading_finished_handler(self) -> None:
        """
        Remove the loading finished event handler.
        """
        self.tab.remove_handlers(
            cdp.network.LoadingFinished, self._loading_finished_handler
        )

    async def __aenter__(self):  # type: ignore
        """
        Enter the context manager, adding request and response handlers.
        """
        await self._setup()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """
        Exit the context manager, removing request and response handlers.
        """
        self._teardown()

    async def _setup(self) -> None:
        self.tab.add_handler(cdp.network.RequestWillBeSent, self._request_handler)
        self.tab.add_handler(cdp.network.ResponseReceived, self._response_handler)
        self.tab.add_handler(
            cdp.network.LoadingFinished, self._loading_finished_handler
        )

    def _teardown(self) -> None:
        self._remove_request_handler()
        self._remove_response_handler()
        self._remove_loading_finished_handler()

    async def reset(self) -> None:
        """
        Resets the internal state, allowing the expectation to be reused.
        """
        self.request_future = asyncio.Future()
        self.response_future = asyncio.Future()
        self.loading_finished_future = asyncio.Future()
        self.request_id = None

        # Reset multiple expectation state
        self.requests = []
        self.responses = []
        self.loading_finished_events = []
        self.request_ids = []
        self.matched_patterns = set()
        self.request_id_to_pattern = {}
        self.matched_patterns_responses = set()
        self.matched_patterns_loading = set()
        if self.count is not None:
            self.all_done_future = asyncio.Future()

        self._teardown()
        await self._setup()

    @property
    async def request(self) -> cdp.network.Request:
        """
        Get the matched request. Only for single expectation mode.
        :return: The matched request.
        :rtype: cdp.network.Request
        """
        return (await self.request_future).request

    @property
    async def response(self) -> cdp.network.Response:
        """
        Get the matched response. Only for single expectation mode.
        :return: The matched response.
        :rtype: cdp.network.Response
        """
        return (await self.response_future).response

    @property
    async def response_body(self) -> tuple[str, bool]:
        """
        Get the body of the matched response. Only for single expectation mode.
        :return: The response body.
        :rtype: str
        """
        request_id = (await self.response_future).request_id
        await (
            self.loading_finished_future
        )  # Ensure the loading is finished before fetching the body
        body = await self.tab.send(cdp.network.get_response_body(request_id=request_id))
        return body

    async def wait_for_all(self) -> None:
        """
        Wait for all patterns to match at least once. Only for multiple expectation mode.
        When using multiple patterns, waits until each pattern has matched at least once.
        """
        if self.count is None:
            raise ValueError("wait_for_all() can only be used with count parameter")
        if self.all_done_future:
            await self.all_done_future

    async def get_all_requests(self) -> list[cdp.network.Request]:
        """
        Get all matched requests. Only for multiple expectation mode.
        :return: List of matched requests.
        :rtype: list[cdp.network.Request]
        """
        await self.wait_for_all()
        return [event.request for event in self.requests]

    async def get_all_responses(self) -> list[cdp.network.Response]:
        """
        Get all matched responses. Only for multiple expectation mode.
        :return: List of matched responses.
        :rtype: list[cdp.network.Response]
        """
        await self.wait_for_all()
        return [event.response for event in self.responses]

    async def get_all_response_bodies(self) -> list[tuple[str, bool]]:
        """
        Get all response bodies. Only for multiple expectation mode.
        :return: List of response bodies.
        :rtype: list[tuple[str, bool]]
        """
        await self.wait_for_all()
        bodies = []
        for response_event in self.responses:
            body = await self.tab.send(cdp.network.get_response_body(request_id=response_event.request_id))
            bodies.append(body)
        return bodies


class RequestExpectation(BaseRequestExpectation):
    """
    Class for handling request expectations.
    This class extends `BaseRequestExpectation` and provides a property to access the matched request.
    :param tab: The Tab instance to monitor.
    :type tab: Tab
    :param url_pattern: The URL pattern(s) to match requests. Can be a single pattern or a list of patterns.
                        When multiple patterns are provided with count parameter, waits until ALL patterns have matched at least once.
    :type url_pattern: Union[str, re.Pattern[str], list[Union[str, re.Pattern[str]]]]
    :param count: Enable multiple expectation mode. When set with multiple patterns, waits until all patterns have matched.
                  If None, waits for one match (single expectation mode).
    :type count: Union[int, None]
    """

    @property
    async def value(self) -> cdp.network.RequestWillBeSent:
        """
        Get the matched request event. Only for single expectation mode.
        :return: The matched request event.
        :rtype: cdp.network.RequestWillBeSent
        """
        return await self.request_future

    async def get_all_values(self) -> list[cdp.network.RequestWillBeSent]:
        """
        Get all matched request events. Only for multiple expectation mode.
        :return: List of matched request events.
        :rtype: list[cdp.network.RequestWillBeSent]
        """
        await self.wait_for_all()
        return self.requests


class ResponseExpectation(BaseRequestExpectation):
    """
    Class for handling response expectations.
    This class extends `BaseRequestExpectation` and provides a property to access the matched response.
    :param tab: The Tab instance to monitor.
    :type tab: Tab
    :param url_pattern: The URL pattern(s) to match responses. Can be a single pattern or a list of patterns.
                        When multiple patterns are provided with count parameter, waits until ALL patterns have matched at least once.
    :type url_pattern: Union[str, re.Pattern[str], list[Union[str, re.Pattern[str]]]]
    :param count: Enable multiple expectation mode. When set with multiple patterns, waits until all patterns have matched.
                  If None, waits for one match (single expectation mode).
    :type count: Union[int, None]
    """

    @property
    async def value(self) -> cdp.network.ResponseReceived:
        """
        Get the matched response event. Only for single expectation mode.
        :return: The matched response event.
        :rtype: cdp.network.ResponseReceived
        """
        return await self.response_future

    async def get_all_values(self) -> list[cdp.network.ResponseReceived]:
        """
        Get all matched response events. Only for multiple expectation mode.
        :return: List of matched response events.
        :rtype: list[cdp.network.ResponseReceived]
        """
        await self.wait_for_all()
        return self.responses


class DownloadExpectation:
    def __init__(self, tab: Connection):
        self.tab = tab
        self.future: asyncio.Future[cdp.browser.DownloadWillBegin] = asyncio.Future()
        # TODO: Improve
        self.default_behavior = (
            self.tab._download_behavior[0] if self.tab._download_behavior else "default"
        )
        self.download_path = (
            self.tab._download_behavior[1]
            if self.tab._download_behavior and len(self.tab._download_behavior) > 1
            else None
        )

    async def _handler(self, event: cdp.browser.DownloadWillBegin) -> None:
        self._remove_handler()
        self.future.set_result(event)

    def _remove_handler(self) -> None:
        self.tab.remove_handlers(cdp.browser.DownloadWillBegin, self._handler)

    async def __aenter__(self) -> "DownloadExpectation":
        """
        Enter the context manager, adding download handler, set download behavior to deny.
        """
        await self.tab.send(
            cdp.browser.set_download_behavior(behavior="deny", events_enabled=True)
        )
        self.tab.add_handler(cdp.browser.DownloadWillBegin, self._handler)
        return self

    async def __aexit__(self, *args: Any) -> None:
        """
        Exit the context manager, removing handler, set download behavior to default.
        """
        await self.tab.send(
            cdp.browser.set_download_behavior(
                behavior=self.default_behavior, download_path=self.download_path
            )
        )
        self._remove_handler()

    @property
    async def value(self) -> cdp.browser.DownloadWillBegin:
        return await self.future
