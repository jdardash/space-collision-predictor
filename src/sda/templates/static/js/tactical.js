// Tactical overlays for the WorldView globe: fading trails, a screen-space
// label arbiter, camera-mode poses and sensor field-of-regard geometry.
//
// The trail renderer, label arbiter and tracked-camera design are adapted from
// gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view, MIT license,
// Copyright (c) 2026 Bilawal Sidhu).
//
// Frames: positions arrive as Cesium Cartesians in the Earth-fixed frame the
// scene renders in. The sensor helpers below work in geodetic degrees on a
// spherical Earth of the WGS72 equatorial radius, which is the same figure
// sda.constants uses, so a footprint drawn here lines up with what
// sda.sensors computes on the server to within the sphere approximation.

(function (global) {
  'use strict';

  var DEG = Math.PI / 180.0;
  var EARTH_RADIUS_KM = 6378.135; // WGS72 equatorial radius (sda.constants)

  // ---------------------------------------------------------------------
  // Sensor field-of-regard geometry
  // ---------------------------------------------------------------------

  // Great-circle destination: walk `centralAngleRad` from (latDeg, lonDeg)
  // along `azimuthRad` measured clockwise from north.
  function destinationPoint(latDeg, lonDeg, centralAngleRad, azimuthRad) {
    var lat1 = latDeg * DEG;
    var lon1 = lonDeg * DEG;
    var sinLat1 = Math.sin(lat1);
    var cosLat1 = Math.cos(lat1);
    var sinG = Math.sin(centralAngleRad);
    var cosG = Math.cos(centralAngleRad);
    var sinLat2 = sinLat1 * cosG + cosLat1 * sinG * Math.cos(azimuthRad);
    sinLat2 = Math.max(-1, Math.min(1, sinLat2));
    var lat2 = Math.asin(sinLat2);
    var lon2 = lon1 + Math.atan2(
      Math.sin(azimuthRad) * sinG * cosLat1,
      cosG - sinLat1 * sinLat2
    );
    return {
      lat: lat2 / DEG,
      lon: (((lon2 / DEG) + 540) % 360) - 180
    };
  }

  // Ground radius of the region a sensor can see at one target altitude.
  //
  // Two limits apply and the tighter one wins. The elevation mask gives
  //   gamma_elev = acos(Re * cos(e) / r) - e
  // and the slant-range limit gives
  //   cos(gamma_range) = (Re^2 + r^2 - Rmax^2) / (2 * Re * r)
  // with Re the Earth radius, r = Re + altitude and e the minimum elevation.
  // The site's own altitude (at most a couple of km) is neglected.
  function fieldOfRegardRadiusKm(sensor, targetAltKm) {
    var re = EARTH_RADIUS_KM;
    var r = re + Math.max(0, targetAltKm || 0);
    var elevDeg = (sensor.min_elevation_deg == null) ? 5.0 : sensor.min_elevation_deg;
    var e = elevDeg * DEG;
    var cosArg = re * Math.cos(e) / r;
    if (!(cosArg < 1)) return 0; // the shell never clears the elevation mask
    var gamma = Math.acos(cosArg) - e;
    var rmax = sensor.max_range_km || 0;
    if (rmax > 0) {
      var cosRange = (re * re + r * r - rmax * rmax) / (2 * re * r);
      if (cosRange >= 1) return 0; // the shell is beyond the sensor's reach
      gamma = Math.min(gamma, cosRange <= -1 ? Math.PI : Math.acos(cosRange));
    }
    return gamma > 0 ? gamma * re : 0;
  }

  // A drawable footprint: the rim of the field of regard at one altitude,
  // plus the azimuth wedge bounds when the site does not sweep all azimuths.
  function sensorFootprint(sensor, targetAltKm, steps) {
    var radiusKm = fieldOfRegardRadiusKm(sensor, targetAltKm);
    if (!(radiusKm > 0)) return null;
    var gamma = radiusKm / EARTH_RADIUS_KM;

    var wedge = (sensor.azimuth_min_deg != null && sensor.azimuth_max_deg != null);
    var azMin = wedge ? sensor.azimuth_min_deg : 0;
    var azMax = wedge ? sensor.azimuth_max_deg : 360;
    if (wedge && azMax <= azMin) azMax += 360; // the wedge wraps through north

    var n = Math.max(12, steps || 72);
    var last = wedge ? n : n - 1; // a full sweep closes on itself
    var rim = [];
    for (var i = 0; i <= last; i++) {
      var az = azMin + (azMax - azMin) * (i / n);
      var p = destinationPoint(sensor.latitude_deg, sensor.longitude_deg, gamma, az * DEG);
      rim.push(p.lon, p.lat);
    }
    return {
      radiusKm: radiusKm,
      rim: rim,
      wedge: wedge,
      azMinDeg: azMin,
      azMaxDeg: azMax,
      altKm: targetAltKm
    };
  }

  // Spokes from the site out to the rim, drawn so the footprint reads as a
  // cone rather than a floating disc.
  function footprintSpokes(sensor, footprint, count) {
    if (!footprint) return [];
    var gamma = footprint.radiusKm / EARTH_RADIUS_KM;
    var spokes = [];
    var n = Math.max(2, count || 4);
    var span = footprint.azMaxDeg - footprint.azMinDeg;
    var divisor = footprint.wedge ? (n - 1) : n;
    for (var i = 0; i < n; i++) {
      var az = footprint.azMinDeg + span * (i / divisor);
      var p = destinationPoint(sensor.latitude_deg, sensor.longitude_deg, gamma, az * DEG);
      spokes.push([sensor.longitude_deg, sensor.latitude_deg, p.lon, p.lat]);
    }
    return spokes;
  }

  // ---------------------------------------------------------------------
  // Trails
  // ---------------------------------------------------------------------

  // A ring buffer of recent positions per tracked object, drawn as one
  // polyline whose vertex alpha ramps from the oldest sample to the newest.
  // Entity polylines cannot fade per vertex, so this uses PolylineGeometry
  // with colorsPerVertex and PolylineColorAppearance instead.
  function createTrailStore(Cesium, scene, options) {
    var opts = options || {};
    var store = {
      maxPoints: opts.maxPoints || 60,
      width: opts.width || 2.0,
      tracks: {},
      count: 0,
      collection: scene.primitives.add(new Cesium.PrimitiveCollection())
    };
    store.collection.show = false;

    store.setShow = function (visible) { store.collection.show = !!visible; };

    store.setMaxPoints = function (n) {
      store.maxPoints = Math.max(2, Math.min(600, n | 0));
      for (var key in store.tracks) {
        var track = store.tracks[key];
        while (track.positions.length > store.maxPoints) track.positions.shift();
        track.dirty = true;
      }
    };

    store.push = function (key, position, color) {
      if (!position) return;
      var track = store.tracks[key];
      if (!track) {
        track = { positions: [], color: color || Cesium.Color.CYAN, primitive: null, dirty: true };
        store.tracks[key] = track;
      }
      if (color) track.color = color;
      var last = track.positions[track.positions.length - 1];
      if (last && Cesium.Cartesian3.distanceSquared(last, position) < 1.0) return; // stationary
      track.positions.push(Cesium.Cartesian3.clone(position));
      while (track.positions.length > store.maxPoints) track.positions.shift();
      track.dirty = true;
    };

    store.drop = function (key) {
      var track = store.tracks[key];
      if (!track) return;
      if (track.primitive) store.collection.remove(track.primitive);
      delete store.tracks[key];
    };

    // Forget every track whose key is not in `keep` (an object used as a set).
    store.prune = function (keep) {
      for (var key in store.tracks) {
        if (!keep[key]) store.drop(key);
      }
    };

    store.clear = function () {
      store.collection.removeAll();
      store.tracks = {};
      store.count = 0;
    };

    // Rebuild the primitives whose buffers changed. Called at a low rate, not
    // every frame: a Primitive is cheap to make at this vertex count but not
    // free at 60 Hz.
    store.sync = function () {
      var live = 0;
      for (var key in store.tracks) {
        var track = store.tracks[key];
        if (track.positions.length >= 2) live++;
        if (!track.dirty) continue;
        track.dirty = false;
        if (track.primitive) {
          store.collection.remove(track.primitive);
          track.primitive = null;
        }
        var n = track.positions.length;
        if (n < 2) continue;
        var colors = new Array(n);
        for (var i = 0; i < n; i++) {
          var f = i / (n - 1);
          colors[i] = Cesium.Color.fromAlpha(track.color, 0.04 + 0.86 * f * f);
        }
        track.primitive = store.collection.add(new Cesium.Primitive({
          geometryInstances: new Cesium.GeometryInstance({
            geometry: new Cesium.PolylineGeometry({
              positions: track.positions.slice(),
              width: store.width,
              colors: colors,
              colorsPerVertex: true,
              arcType: Cesium.ArcType.NONE,
              vertexFormat: Cesium.PolylineColorAppearance.VERTEX_FORMAT
            })
          }),
          appearance: new Cesium.PolylineColorAppearance({ translucent: true }),
          asynchronous: false
        }));
      }
      store.count = live;
      return live;
    };

    return store;
  }

  // ---------------------------------------------------------------------
  // Label declutter
  // ---------------------------------------------------------------------

  // Screen-space arbiter: project each candidate, drop the ones behind the
  // globe or off-screen, then let the first label to claim a screen cell keep
  // it. Entries are sorted by priority first, so a selected or tracked object
  // always wins its cell.
  function createLabelArbiter(Cesium, options) {
    var opts = options || {};
    var transforms = Cesium.SceneTransforms || {};
    var api = {
      cellPx: opts.cellPx || 36,
      hidden: 0,
      considered: 0,
      // The older name for this call carries a datum token this project does
      // not write, and it is deprecated in Cesium 1.119 anyway. If the method
      // is missing the arbiter simply stands down and every label stays up.
      available: typeof transforms.worldToWindowCoordinates === 'function'
    };
    var scratch = new Cesium.Cartesian2();

    api.run = function (scene, entries) {
      api.considered = entries.length;
      if (!api.available || !entries.length) { api.hidden = 0; return 0; }
      var toWindow = transforms.worldToWindowCoordinates;
      var occluder = new Cesium.EllipsoidalOccluder(scene.globe.ellipsoid, scene.camera.positionWC);
      entries.sort(function (a, b) { return a.priority - b.priority; });

      var width = scene.canvas.clientWidth;
      var height = scene.canvas.clientHeight;
      var margin = 48;
      var claimed = {};
      var hidden = 0;

      for (var i = 0; i < entries.length; i++) {
        var entry = entries[i];
        var label = entry.label;
        if (!label || !entry.position) continue;
        if (!occluder.isPointVisible(entry.position)) { label.show = false; hidden++; continue; }
        var win = toWindow(scene, entry.position, scratch);
        if (!win || win.x < -margin || win.y < -margin ||
            win.x > width + margin || win.y > height + margin) {
          label.show = false; hidden++; continue;
        }
        var cell = Math.floor(win.x / api.cellPx) + ':' + Math.floor(win.y / api.cellPx);
        if (claimed[cell]) { label.show = false; hidden++; continue; }
        claimed[cell] = true;
        label.show = true;
      }
      api.hidden = hidden;
      return hidden;
    };

    return api;
  }

  // ---------------------------------------------------------------------
  // Camera poses
  // ---------------------------------------------------------------------

  // The up vector for a camera looking along `direction` from a point above
  // `position`: the radial direction with its along-view component removed.
  function orthogonalUp(Cesium, position, direction) {
    var radial = Cesium.Cartesian3.normalize(position, new Cesium.Cartesian3());
    var along = Cesium.Cartesian3.multiplyByScalar(
      direction, Cesium.Cartesian3.dot(radial, direction), new Cesium.Cartesian3());
    var up = Cesium.Cartesian3.subtract(radial, along, new Cesium.Cartesian3());
    if (Cesium.Cartesian3.magnitude(up) < 1.0e-6) {
      return Cesium.Cartesian3.clone(Cesium.Cartesian3.UNIT_Z, new Cesium.Cartesian3());
    }
    return Cesium.Cartesian3.normalize(up, up);
  }

  // Chase: sit behind the object along its velocity vector and slightly above
  // it, looking forward through the object.
  function chasePose(Cesium, position, forward, rangeM, liftFraction) {
    var back = Cesium.Cartesian3.multiplyByScalar(forward, -rangeM, new Cesium.Cartesian3());
    var radial = Cesium.Cartesian3.normalize(position, new Cesium.Cartesian3());
    var lift = Cesium.Cartesian3.multiplyByScalar(
      radial, rangeM * (liftFraction == null ? 0.25 : liftFraction), new Cesium.Cartesian3());
    var destination = Cesium.Cartesian3.add(position, back, new Cesium.Cartesian3());
    Cesium.Cartesian3.add(destination, lift, destination);
    var direction = Cesium.Cartesian3.subtract(position, destination, new Cesium.Cartesian3());
    Cesium.Cartesian3.normalize(direction, direction);
    return {
      destination: destination,
      direction: direction,
      up: orthogonalUp(Cesium, destination, direction)
    };
  }

  // Cockpit: ride the object itself, looking along the velocity vector.
  function cockpitPose(Cesium, position, forward, aheadM) {
    var offset = Cesium.Cartesian3.multiplyByScalar(
      forward, aheadM == null ? 0 : aheadM, new Cesium.Cartesian3());
    var destination = Cesium.Cartesian3.add(position, offset, new Cesium.Cartesian3());
    var direction = Cesium.Cartesian3.clone(forward, new Cesium.Cartesian3());
    return {
      destination: destination,
      direction: direction,
      up: orthogonalUp(Cesium, destination, direction)
    };
  }

  // Orbit-around: circle the object in its local horizontal plane at a fixed
  // elevation above it, always looking back at it.
  function orbitPose(Cesium, position, angleRad, rangeM, pitchRad) {
    var frame = Cesium.Transforms.eastNorthUpToFixedFrame(position);
    var east = Cesium.Cartesian3.fromCartesian4(
      Cesium.Matrix4.getColumn(frame, 0, new Cesium.Cartesian4()));
    var north = Cesium.Cartesian3.fromCartesian4(
      Cesium.Matrix4.getColumn(frame, 1, new Cesium.Cartesian4()));
    var up = Cesium.Cartesian3.fromCartesian4(
      Cesium.Matrix4.getColumn(frame, 2, new Cesium.Cartesian4()));

    var horizontal = Math.cos(pitchRad) * rangeM;
    var offset = Cesium.Cartesian3.multiplyByScalar(
      east, Math.cos(angleRad) * horizontal, new Cesium.Cartesian3());
    Cesium.Cartesian3.add(offset, Cesium.Cartesian3.multiplyByScalar(
      north, Math.sin(angleRad) * horizontal, new Cesium.Cartesian3()), offset);
    Cesium.Cartesian3.add(offset, Cesium.Cartesian3.multiplyByScalar(
      up, Math.sin(pitchRad) * rangeM, new Cesium.Cartesian3()), offset);

    var destination = Cesium.Cartesian3.add(position, offset, new Cesium.Cartesian3());
    var direction = Cesium.Cartesian3.subtract(position, destination, new Cesium.Cartesian3());
    Cesium.Cartesian3.normalize(direction, direction);
    return {
      destination: destination,
      direction: direction,
      up: orthogonalUp(Cesium, destination, direction)
    };
  }

  // The unit velocity direction implied by two successive positions. Returns
  // null when the object has not moved, so the caller can hold its last
  // heading instead of spinning the camera on numerical noise.
  function forwardFrom(Cesium, previous, current) {
    if (!previous || !current) return null;
    var delta = Cesium.Cartesian3.subtract(current, previous, new Cesium.Cartesian3());
    if (Cesium.Cartesian3.magnitude(delta) < 1.0) return null;
    return Cesium.Cartesian3.normalize(delta, delta);
  }

  global.WorldViewTactical = {
    DEG: DEG,
    EARTH_RADIUS_KM: EARTH_RADIUS_KM,
    destinationPoint: destinationPoint,
    fieldOfRegardRadiusKm: fieldOfRegardRadiusKm,
    sensorFootprint: sensorFootprint,
    footprintSpokes: footprintSpokes,
    createTrailStore: createTrailStore,
    createLabelArbiter: createLabelArbiter,
    orthogonalUp: orthogonalUp,
    chasePose: chasePose,
    cockpitPose: cockpitPose,
    orbitPose: orbitPose,
    forwardFrom: forwardFrom
  };
})(this);
