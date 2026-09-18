/*
 * ATTENTION: An "eval-source-map" devtool has been used.
 * This devtool is neither made for production nor for readable output files.
 * It uses "eval()" calls to create a separate source file with attached SourceMaps in the browser devtools.
 * If you are trying to read the output file, select a different devtool (https://webpack.js.org/configuration/devtool/)
 * or disable the default devtool with "devtool: false".
 * If you are looking for production-ready output files, see mode: "production" (https://webpack.js.org/configuration/mode/).
 */
(() => {
var exports = {};
exports.id = "app/api/weather/route";
exports.ids = ["app/api/weather/route"];
exports.modules = {

/***/ "(rsc)/./app/api/weather/route.ts":
/*!**********************************!*\
  !*** ./app/api/weather/route.ts ***!
  \**********************************/
/***/ ((__unused_webpack_module, __webpack_exports__, __webpack_require__) => {

"use strict";
eval("__webpack_require__.r(__webpack_exports__);\n/* harmony export */ __webpack_require__.d(__webpack_exports__, {\n/* harmony export */   GET: () => (/* binding */ GET),\n/* harmony export */   dynamic: () => (/* binding */ dynamic)\n/* harmony export */ });\n/* harmony import */ var next_server__WEBPACK_IMPORTED_MODULE_0__ = __webpack_require__(/*! next/server */ \"(rsc)/./node_modules/next/dist/api/server.js\");\n\n/**\n * Weather for the *visitor's* location.\n *\n * On Vercel, the platform adds geo headers to every request\n * (`x-vercel-ip-latitude` / `-longitude` / `-city`) — no permission prompt, no\n * third-party service. We read those and fetch the weather from open-meteo\n * (free, keyless).\n *\n * When there are no geo headers (localhost / off Vercel), we DON'T invent a\n * city — we return `{ current: null, city: \"your town\" }`, and the panel shows\n * \"your town\" with a \"-\" temperature.\n */ const dynamic = \"force-dynamic\"; // per-visitor: reads geo headers\nasync function GET(request) {\n    const h = request.headers;\n    const lat = h.get(\"x-vercel-ip-latitude\");\n    const lon = h.get(\"x-vercel-ip-longitude\");\n    // No location available — don't guess.\n    if (!lat || !lon) {\n        return next_server__WEBPACK_IMPORTED_MODULE_0__.NextResponse.json({\n            current: null,\n            city: \"your town\"\n        });\n    }\n    const cityHeader = h.get(\"x-vercel-ip-city\");\n    const city = cityHeader ? decodeURIComponent(cityHeader) : \"your town\";\n    try {\n        const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code`;\n        // Cache per-coordinate for 10 min so repeat visitors from the same city\n        // don't each trigger a fresh upstream call.\n        const r = await fetch(url, {\n            next: {\n                revalidate: 600\n            }\n        });\n        const d = await r.json();\n        return next_server__WEBPACK_IMPORTED_MODULE_0__.NextResponse.json({\n            current: d.current ?? null,\n            city\n        });\n    } catch  {\n        return next_server__WEBPACK_IMPORTED_MODULE_0__.NextResponse.json({\n            current: null,\n            city\n        });\n    }\n}\n//# sourceURL=[module]\n//# sourceMappingURL=data:application/json;charset=utf-8;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiKHJzYykvLi9hcHAvYXBpL3dlYXRoZXIvcm91dGUudHMiLCJtYXBwaW5ncyI6Ijs7Ozs7O0FBQTJDO0FBRTNDOzs7Ozs7Ozs7OztDQVdDLEdBQ00sTUFBTUMsVUFBVSxnQkFBZ0IsQ0FBQyxpQ0FBaUM7QUFFbEUsZUFBZUMsSUFBSUMsT0FBZ0I7SUFDeEMsTUFBTUMsSUFBSUQsUUFBUUUsT0FBTztJQUN6QixNQUFNQyxNQUFNRixFQUFFRyxHQUFHLENBQUM7SUFDbEIsTUFBTUMsTUFBTUosRUFBRUcsR0FBRyxDQUFDO0lBRWxCLHVDQUF1QztJQUN2QyxJQUFJLENBQUNELE9BQU8sQ0FBQ0UsS0FBSztRQUNoQixPQUFPUixxREFBWUEsQ0FBQ1MsSUFBSSxDQUFDO1lBQUVDLFNBQVM7WUFBTUMsTUFBTTtRQUFZO0lBQzlEO0lBRUEsTUFBTUMsYUFBYVIsRUFBRUcsR0FBRyxDQUFDO0lBQ3pCLE1BQU1JLE9BQU9DLGFBQWFDLG1CQUFtQkQsY0FBYztJQUUzRCxJQUFJO1FBQ0YsTUFBTUUsTUFBTSxDQUFDLGdEQUFnRCxFQUFFUixJQUFJLFdBQVcsRUFBRUUsSUFBSSxvQ0FBb0MsQ0FBQztRQUN6SCx3RUFBd0U7UUFDeEUsNENBQTRDO1FBQzVDLE1BQU1PLElBQUksTUFBTUMsTUFBTUYsS0FBSztZQUFFRyxNQUFNO2dCQUFFQyxZQUFZO1lBQUk7UUFBRTtRQUN2RCxNQUFNQyxJQUFJLE1BQU1KLEVBQUVOLElBQUk7UUFDdEIsT0FBT1QscURBQVlBLENBQUNTLElBQUksQ0FBQztZQUFFQyxTQUFTUyxFQUFFVCxPQUFPLElBQUk7WUFBTUM7UUFBSztJQUM5RCxFQUFFLE9BQU07UUFDTixPQUFPWCxxREFBWUEsQ0FBQ1MsSUFBSSxDQUFDO1lBQUVDLFNBQVM7WUFBTUM7UUFBSztJQUNqRDtBQUNGIiwic291cmNlcyI6WyIvaG9tZS9pb2FubmlzYi9EZXZlbG9wbWVudC9BcGV4L2Zyb250ZW5kL2FwcC9hcGkvd2VhdGhlci9yb3V0ZS50cyJdLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBOZXh0UmVzcG9uc2UgfSBmcm9tIFwibmV4dC9zZXJ2ZXJcIjtcblxuLyoqXG4gKiBXZWF0aGVyIGZvciB0aGUgKnZpc2l0b3IncyogbG9jYXRpb24uXG4gKlxuICogT24gVmVyY2VsLCB0aGUgcGxhdGZvcm0gYWRkcyBnZW8gaGVhZGVycyB0byBldmVyeSByZXF1ZXN0XG4gKiAoYHgtdmVyY2VsLWlwLWxhdGl0dWRlYCAvIGAtbG9uZ2l0dWRlYCAvIGAtY2l0eWApIOKAlCBubyBwZXJtaXNzaW9uIHByb21wdCwgbm9cbiAqIHRoaXJkLXBhcnR5IHNlcnZpY2UuIFdlIHJlYWQgdGhvc2UgYW5kIGZldGNoIHRoZSB3ZWF0aGVyIGZyb20gb3Blbi1tZXRlb1xuICogKGZyZWUsIGtleWxlc3MpLlxuICpcbiAqIFdoZW4gdGhlcmUgYXJlIG5vIGdlbyBoZWFkZXJzIChsb2NhbGhvc3QgLyBvZmYgVmVyY2VsKSwgd2UgRE9OJ1QgaW52ZW50IGFcbiAqIGNpdHkg4oCUIHdlIHJldHVybiBgeyBjdXJyZW50OiBudWxsLCBjaXR5OiBcInlvdXIgdG93blwiIH1gLCBhbmQgdGhlIHBhbmVsIHNob3dzXG4gKiBcInlvdXIgdG93blwiIHdpdGggYSBcIi1cIiB0ZW1wZXJhdHVyZS5cbiAqL1xuZXhwb3J0IGNvbnN0IGR5bmFtaWMgPSBcImZvcmNlLWR5bmFtaWNcIjsgLy8gcGVyLXZpc2l0b3I6IHJlYWRzIGdlbyBoZWFkZXJzXG5cbmV4cG9ydCBhc3luYyBmdW5jdGlvbiBHRVQocmVxdWVzdDogUmVxdWVzdCkge1xuICBjb25zdCBoID0gcmVxdWVzdC5oZWFkZXJzO1xuICBjb25zdCBsYXQgPSBoLmdldChcIngtdmVyY2VsLWlwLWxhdGl0dWRlXCIpO1xuICBjb25zdCBsb24gPSBoLmdldChcIngtdmVyY2VsLWlwLWxvbmdpdHVkZVwiKTtcblxuICAvLyBObyBsb2NhdGlvbiBhdmFpbGFibGUg4oCUIGRvbid0IGd1ZXNzLlxuICBpZiAoIWxhdCB8fCAhbG9uKSB7XG4gICAgcmV0dXJuIE5leHRSZXNwb25zZS5qc29uKHsgY3VycmVudDogbnVsbCwgY2l0eTogXCJ5b3VyIHRvd25cIiB9KTtcbiAgfVxuXG4gIGNvbnN0IGNpdHlIZWFkZXIgPSBoLmdldChcIngtdmVyY2VsLWlwLWNpdHlcIik7XG4gIGNvbnN0IGNpdHkgPSBjaXR5SGVhZGVyID8gZGVjb2RlVVJJQ29tcG9uZW50KGNpdHlIZWFkZXIpIDogXCJ5b3VyIHRvd25cIjtcblxuICB0cnkge1xuICAgIGNvbnN0IHVybCA9IGBodHRwczovL2FwaS5vcGVuLW1ldGVvLmNvbS92MS9mb3JlY2FzdD9sYXRpdHVkZT0ke2xhdH0mbG9uZ2l0dWRlPSR7bG9ufSZjdXJyZW50PXRlbXBlcmF0dXJlXzJtLHdlYXRoZXJfY29kZWA7XG4gICAgLy8gQ2FjaGUgcGVyLWNvb3JkaW5hdGUgZm9yIDEwIG1pbiBzbyByZXBlYXQgdmlzaXRvcnMgZnJvbSB0aGUgc2FtZSBjaXR5XG4gICAgLy8gZG9uJ3QgZWFjaCB0cmlnZ2VyIGEgZnJlc2ggdXBzdHJlYW0gY2FsbC5cbiAgICBjb25zdCByID0gYXdhaXQgZmV0Y2godXJsLCB7IG5leHQ6IHsgcmV2YWxpZGF0ZTogNjAwIH0gfSk7XG4gICAgY29uc3QgZCA9IGF3YWl0IHIuanNvbigpO1xuICAgIHJldHVybiBOZXh0UmVzcG9uc2UuanNvbih7IGN1cnJlbnQ6IGQuY3VycmVudCA/PyBudWxsLCBjaXR5IH0pO1xuICB9IGNhdGNoIHtcbiAgICByZXR1cm4gTmV4dFJlc3BvbnNlLmpzb24oeyBjdXJyZW50OiBudWxsLCBjaXR5IH0pO1xuICB9XG59XG4iXSwibmFtZXMiOlsiTmV4dFJlc3BvbnNlIiwiZHluYW1pYyIsIkdFVCIsInJlcXVlc3QiLCJoIiwiaGVhZGVycyIsImxhdCIsImdldCIsImxvbiIsImpzb24iLCJjdXJyZW50IiwiY2l0eSIsImNpdHlIZWFkZXIiLCJkZWNvZGVVUklDb21wb25lbnQiLCJ1cmwiLCJyIiwiZmV0Y2giLCJuZXh0IiwicmV2YWxpZGF0ZSIsImQiXSwiaWdub3JlTGlzdCI6W10sInNvdXJjZVJvb3QiOiIifQ==\n//# sourceURL=webpack-internal:///(rsc)/./app/api/weather/route.ts\n");

/***/ }),

/***/ "(rsc)/./node_modules/next/dist/build/webpack/loaders/next-app-loader/index.js?name=app%2Fapi%2Fweather%2Froute&page=%2Fapi%2Fweather%2Froute&appPaths=&pagePath=private-next-app-dir%2Fapi%2Fweather%2Froute.ts&appDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend%2Fapp&pageExtensions=tsx&pageExtensions=ts&pageExtensions=jsx&pageExtensions=js&rootDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend&isDev=true&tsconfigPath=tsconfig.json&basePath=&assetPrefix=&nextConfigOutput=&preferredRegion=&middlewareConfig=e30%3D!":
/*!***********************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************!*\
  !*** ./node_modules/next/dist/build/webpack/loaders/next-app-loader/index.js?name=app%2Fapi%2Fweather%2Froute&page=%2Fapi%2Fweather%2Froute&appPaths=&pagePath=private-next-app-dir%2Fapi%2Fweather%2Froute.ts&appDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend%2Fapp&pageExtensions=tsx&pageExtensions=ts&pageExtensions=jsx&pageExtensions=js&rootDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend&isDev=true&tsconfigPath=tsconfig.json&basePath=&assetPrefix=&nextConfigOutput=&preferredRegion=&middlewareConfig=e30%3D! ***!
  \***********************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************************/
/***/ ((__unused_webpack_module, __webpack_exports__, __webpack_require__) => {

"use strict";
eval("__webpack_require__.r(__webpack_exports__);\n/* harmony export */ __webpack_require__.d(__webpack_exports__, {\n/* harmony export */   patchFetch: () => (/* binding */ patchFetch),\n/* harmony export */   routeModule: () => (/* binding */ routeModule),\n/* harmony export */   serverHooks: () => (/* binding */ serverHooks),\n/* harmony export */   workAsyncStorage: () => (/* binding */ workAsyncStorage),\n/* harmony export */   workUnitAsyncStorage: () => (/* binding */ workUnitAsyncStorage)\n/* harmony export */ });\n/* harmony import */ var next_dist_server_route_modules_app_route_module_compiled__WEBPACK_IMPORTED_MODULE_0__ = __webpack_require__(/*! next/dist/server/route-modules/app-route/module.compiled */ \"(rsc)/./node_modules/next/dist/server/route-modules/app-route/module.compiled.js\");\n/* harmony import */ var next_dist_server_route_modules_app_route_module_compiled__WEBPACK_IMPORTED_MODULE_0___default = /*#__PURE__*/__webpack_require__.n(next_dist_server_route_modules_app_route_module_compiled__WEBPACK_IMPORTED_MODULE_0__);\n/* harmony import */ var next_dist_server_route_kind__WEBPACK_IMPORTED_MODULE_1__ = __webpack_require__(/*! next/dist/server/route-kind */ \"(rsc)/./node_modules/next/dist/server/route-kind.js\");\n/* harmony import */ var next_dist_server_lib_patch_fetch__WEBPACK_IMPORTED_MODULE_2__ = __webpack_require__(/*! next/dist/server/lib/patch-fetch */ \"(rsc)/./node_modules/next/dist/server/lib/patch-fetch.js\");\n/* harmony import */ var next_dist_server_lib_patch_fetch__WEBPACK_IMPORTED_MODULE_2___default = /*#__PURE__*/__webpack_require__.n(next_dist_server_lib_patch_fetch__WEBPACK_IMPORTED_MODULE_2__);\n/* harmony import */ var _home_ioannisb_Development_Apex_frontend_app_api_weather_route_ts__WEBPACK_IMPORTED_MODULE_3__ = __webpack_require__(/*! ./app/api/weather/route.ts */ \"(rsc)/./app/api/weather/route.ts\");\n\n\n\n\n// We inject the nextConfigOutput here so that we can use them in the route\n// module.\nconst nextConfigOutput = \"\"\nconst routeModule = new next_dist_server_route_modules_app_route_module_compiled__WEBPACK_IMPORTED_MODULE_0__.AppRouteRouteModule({\n    definition: {\n        kind: next_dist_server_route_kind__WEBPACK_IMPORTED_MODULE_1__.RouteKind.APP_ROUTE,\n        page: \"/api/weather/route\",\n        pathname: \"/api/weather\",\n        filename: \"route\",\n        bundlePath: \"app/api/weather/route\"\n    },\n    resolvedPagePath: \"/home/ioannisb/Development/Apex/frontend/app/api/weather/route.ts\",\n    nextConfigOutput,\n    userland: _home_ioannisb_Development_Apex_frontend_app_api_weather_route_ts__WEBPACK_IMPORTED_MODULE_3__\n});\n// Pull out the exports that we need to expose from the module. This should\n// be eliminated when we've moved the other routes to the new format. These\n// are used to hook into the route.\nconst { workAsyncStorage, workUnitAsyncStorage, serverHooks } = routeModule;\nfunction patchFetch() {\n    return (0,next_dist_server_lib_patch_fetch__WEBPACK_IMPORTED_MODULE_2__.patchFetch)({\n        workAsyncStorage,\n        workUnitAsyncStorage\n    });\n}\n\n\n//# sourceMappingURL=app-route.js.map//# sourceURL=[module]\n//# sourceMappingURL=data:application/json;charset=utf-8;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiKHJzYykvLi9ub2RlX21vZHVsZXMvbmV4dC9kaXN0L2J1aWxkL3dlYnBhY2svbG9hZGVycy9uZXh0LWFwcC1sb2FkZXIvaW5kZXguanM/bmFtZT1hcHAlMkZhcGklMkZ3ZWF0aGVyJTJGcm91dGUmcGFnZT0lMkZhcGklMkZ3ZWF0aGVyJTJGcm91dGUmYXBwUGF0aHM9JnBhZ2VQYXRoPXByaXZhdGUtbmV4dC1hcHAtZGlyJTJGYXBpJTJGd2VhdGhlciUyRnJvdXRlLnRzJmFwcERpcj0lMkZob21lJTJGaW9hbm5pc2IlMkZEZXZlbG9wbWVudCUyRkFwZXglMkZmcm9udGVuZCUyRmFwcCZwYWdlRXh0ZW5zaW9ucz10c3gmcGFnZUV4dGVuc2lvbnM9dHMmcGFnZUV4dGVuc2lvbnM9anN4JnBhZ2VFeHRlbnNpb25zPWpzJnJvb3REaXI9JTJGaG9tZSUyRmlvYW5uaXNiJTJGRGV2ZWxvcG1lbnQlMkZBcGV4JTJGZnJvbnRlbmQmaXNEZXY9dHJ1ZSZ0c2NvbmZpZ1BhdGg9dHNjb25maWcuanNvbiZiYXNlUGF0aD0mYXNzZXRQcmVmaXg9Jm5leHRDb25maWdPdXRwdXQ9JnByZWZlcnJlZFJlZ2lvbj0mbWlkZGxld2FyZUNvbmZpZz1lMzAlM0QhIiwibWFwcGluZ3MiOiI7Ozs7Ozs7Ozs7Ozs7O0FBQStGO0FBQ3ZDO0FBQ3FCO0FBQ2lCO0FBQzlGO0FBQ0E7QUFDQTtBQUNBLHdCQUF3Qix5R0FBbUI7QUFDM0M7QUFDQSxjQUFjLGtFQUFTO0FBQ3ZCO0FBQ0E7QUFDQTtBQUNBO0FBQ0EsS0FBSztBQUNMO0FBQ0E7QUFDQSxZQUFZO0FBQ1osQ0FBQztBQUNEO0FBQ0E7QUFDQTtBQUNBLFFBQVEsc0RBQXNEO0FBQzlEO0FBQ0EsV0FBVyw0RUFBVztBQUN0QjtBQUNBO0FBQ0EsS0FBSztBQUNMO0FBQzBGOztBQUUxRiIsInNvdXJjZXMiOlsiIl0sInNvdXJjZXNDb250ZW50IjpbImltcG9ydCB7IEFwcFJvdXRlUm91dGVNb2R1bGUgfSBmcm9tIFwibmV4dC9kaXN0L3NlcnZlci9yb3V0ZS1tb2R1bGVzL2FwcC1yb3V0ZS9tb2R1bGUuY29tcGlsZWRcIjtcbmltcG9ydCB7IFJvdXRlS2luZCB9IGZyb20gXCJuZXh0L2Rpc3Qvc2VydmVyL3JvdXRlLWtpbmRcIjtcbmltcG9ydCB7IHBhdGNoRmV0Y2ggYXMgX3BhdGNoRmV0Y2ggfSBmcm9tIFwibmV4dC9kaXN0L3NlcnZlci9saWIvcGF0Y2gtZmV0Y2hcIjtcbmltcG9ydCAqIGFzIHVzZXJsYW5kIGZyb20gXCIvaG9tZS9pb2FubmlzYi9EZXZlbG9wbWVudC9BcGV4L2Zyb250ZW5kL2FwcC9hcGkvd2VhdGhlci9yb3V0ZS50c1wiO1xuLy8gV2UgaW5qZWN0IHRoZSBuZXh0Q29uZmlnT3V0cHV0IGhlcmUgc28gdGhhdCB3ZSBjYW4gdXNlIHRoZW0gaW4gdGhlIHJvdXRlXG4vLyBtb2R1bGUuXG5jb25zdCBuZXh0Q29uZmlnT3V0cHV0ID0gXCJcIlxuY29uc3Qgcm91dGVNb2R1bGUgPSBuZXcgQXBwUm91dGVSb3V0ZU1vZHVsZSh7XG4gICAgZGVmaW5pdGlvbjoge1xuICAgICAgICBraW5kOiBSb3V0ZUtpbmQuQVBQX1JPVVRFLFxuICAgICAgICBwYWdlOiBcIi9hcGkvd2VhdGhlci9yb3V0ZVwiLFxuICAgICAgICBwYXRobmFtZTogXCIvYXBpL3dlYXRoZXJcIixcbiAgICAgICAgZmlsZW5hbWU6IFwicm91dGVcIixcbiAgICAgICAgYnVuZGxlUGF0aDogXCJhcHAvYXBpL3dlYXRoZXIvcm91dGVcIlxuICAgIH0sXG4gICAgcmVzb2x2ZWRQYWdlUGF0aDogXCIvaG9tZS9pb2FubmlzYi9EZXZlbG9wbWVudC9BcGV4L2Zyb250ZW5kL2FwcC9hcGkvd2VhdGhlci9yb3V0ZS50c1wiLFxuICAgIG5leHRDb25maWdPdXRwdXQsXG4gICAgdXNlcmxhbmRcbn0pO1xuLy8gUHVsbCBvdXQgdGhlIGV4cG9ydHMgdGhhdCB3ZSBuZWVkIHRvIGV4cG9zZSBmcm9tIHRoZSBtb2R1bGUuIFRoaXMgc2hvdWxkXG4vLyBiZSBlbGltaW5hdGVkIHdoZW4gd2UndmUgbW92ZWQgdGhlIG90aGVyIHJvdXRlcyB0byB0aGUgbmV3IGZvcm1hdC4gVGhlc2Vcbi8vIGFyZSB1c2VkIHRvIGhvb2sgaW50byB0aGUgcm91dGUuXG5jb25zdCB7IHdvcmtBc3luY1N0b3JhZ2UsIHdvcmtVbml0QXN5bmNTdG9yYWdlLCBzZXJ2ZXJIb29rcyB9ID0gcm91dGVNb2R1bGU7XG5mdW5jdGlvbiBwYXRjaEZldGNoKCkge1xuICAgIHJldHVybiBfcGF0Y2hGZXRjaCh7XG4gICAgICAgIHdvcmtBc3luY1N0b3JhZ2UsXG4gICAgICAgIHdvcmtVbml0QXN5bmNTdG9yYWdlXG4gICAgfSk7XG59XG5leHBvcnQgeyByb3V0ZU1vZHVsZSwgd29ya0FzeW5jU3RvcmFnZSwgd29ya1VuaXRBc3luY1N0b3JhZ2UsIHNlcnZlckhvb2tzLCBwYXRjaEZldGNoLCAgfTtcblxuLy8jIHNvdXJjZU1hcHBpbmdVUkw9YXBwLXJvdXRlLmpzLm1hcCJdLCJuYW1lcyI6W10sImlnbm9yZUxpc3QiOltdLCJzb3VyY2VSb290IjoiIn0=\n//# sourceURL=webpack-internal:///(rsc)/./node_modules/next/dist/build/webpack/loaders/next-app-loader/index.js?name=app%2Fapi%2Fweather%2Froute&page=%2Fapi%2Fweather%2Froute&appPaths=&pagePath=private-next-app-dir%2Fapi%2Fweather%2Froute.ts&appDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend%2Fapp&pageExtensions=tsx&pageExtensions=ts&pageExtensions=jsx&pageExtensions=js&rootDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend&isDev=true&tsconfigPath=tsconfig.json&basePath=&assetPrefix=&nextConfigOutput=&preferredRegion=&middlewareConfig=e30%3D!\n");

/***/ }),

/***/ "(rsc)/./node_modules/next/dist/build/webpack/loaders/next-flight-client-entry-loader.js?server=true!":
/*!******************************************************************************************************!*\
  !*** ./node_modules/next/dist/build/webpack/loaders/next-flight-client-entry-loader.js?server=true! ***!
  \******************************************************************************************************/
/***/ (() => {



/***/ }),

/***/ "(ssr)/./node_modules/next/dist/build/webpack/loaders/next-flight-client-entry-loader.js?server=true!":
/*!******************************************************************************************************!*\
  !*** ./node_modules/next/dist/build/webpack/loaders/next-flight-client-entry-loader.js?server=true! ***!
  \******************************************************************************************************/
/***/ (() => {



/***/ }),

/***/ "../app-render/after-task-async-storage.external":
/*!***********************************************************************************!*\
  !*** external "next/dist/server/app-render/after-task-async-storage.external.js" ***!
  \***********************************************************************************/
/***/ ((module) => {

"use strict";
module.exports = require("next/dist/server/app-render/after-task-async-storage.external.js");

/***/ }),

/***/ "../app-render/work-async-storage.external":
/*!*****************************************************************************!*\
  !*** external "next/dist/server/app-render/work-async-storage.external.js" ***!
  \*****************************************************************************/
/***/ ((module) => {

"use strict";
module.exports = require("next/dist/server/app-render/work-async-storage.external.js");

/***/ }),

/***/ "./work-unit-async-storage.external":
/*!**********************************************************************************!*\
  !*** external "next/dist/server/app-render/work-unit-async-storage.external.js" ***!
  \**********************************************************************************/
/***/ ((module) => {

"use strict";
module.exports = require("next/dist/server/app-render/work-unit-async-storage.external.js");

/***/ }),

/***/ "next/dist/compiled/next-server/app-page.runtime.dev.js":
/*!*************************************************************************!*\
  !*** external "next/dist/compiled/next-server/app-page.runtime.dev.js" ***!
  \*************************************************************************/
/***/ ((module) => {

"use strict";
module.exports = require("next/dist/compiled/next-server/app-page.runtime.dev.js");

/***/ }),

/***/ "next/dist/compiled/next-server/app-route.runtime.dev.js":
/*!**************************************************************************!*\
  !*** external "next/dist/compiled/next-server/app-route.runtime.dev.js" ***!
  \**************************************************************************/
/***/ ((module) => {

"use strict";
module.exports = require("next/dist/compiled/next-server/app-route.runtime.dev.js");

/***/ })

};
;

// load runtime
var __webpack_require__ = require("../../../webpack-runtime.js");
__webpack_require__.C(exports);
var __webpack_exec__ = (moduleId) => (__webpack_require__(__webpack_require__.s = moduleId))
var __webpack_exports__ = __webpack_require__.X(0, ["vendor-chunks/next"], () => (__webpack_exec__("(rsc)/./node_modules/next/dist/build/webpack/loaders/next-app-loader/index.js?name=app%2Fapi%2Fweather%2Froute&page=%2Fapi%2Fweather%2Froute&appPaths=&pagePath=private-next-app-dir%2Fapi%2Fweather%2Froute.ts&appDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend%2Fapp&pageExtensions=tsx&pageExtensions=ts&pageExtensions=jsx&pageExtensions=js&rootDir=%2Fhome%2Fioannisb%2FDevelopment%2FApex%2Ffrontend&isDev=true&tsconfigPath=tsconfig.json&basePath=&assetPrefix=&nextConfigOutput=&preferredRegion=&middlewareConfig=e30%3D!")));
module.exports = __webpack_exports__;

})();