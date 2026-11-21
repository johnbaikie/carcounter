// TF-Luna rain hood: a sleeve the sensor slides into, with a visor past the lenses,
// a drip groove, an open floor in front of the sensor (drains), a back plate with a
// cable window, and a screw flange for a post or board.
//
// Modelled in PRINT orientation: back plate flat on the bed, beam pointing up (+Z).
// Installed, +Y is up and the beam is horizontal. No supports needed.
//
// MEASURE YOUR SENSOR and adjust luna_* below (datasheet: 35 x 21.25 x 13.5 mm).
// Print a test first and check the fit: tight -> raise `clear`, loose -> lower it.

/* [Sensor] */
luna_w = 35.0;      // across the front face (the two lenses sit side by side along this)
luna_h = 21.25;     // height of the front face
luna_d = 13.5;      // front-to-back depth (excluding the cable plug)
clear  = 0.35;      // gap per side for a snug slide fit
rib    = 0.35;      // crush ribs on the side walls hold the sensor (0 = none)

/* [Hood] */
wall     = 2.0;     // wall thickness (2 mm = 4-5 perimeters at 0.4 nozzle)
back     = 2.4;     // back plate thickness
hood_len = 30;      // how far the visor reaches past the lenses
top_gap  = 4.0;     // headroom above the sensor at its front face
slope    = 4;       // visor slopes down this many degrees toward the front (sheds water)
groove   = 1.5;     // drip groove under the visor's front edge (0 = none)

/* [Back plate and mounting] */
win_w      = 26;    // cable window in the back plate (leaves a ledge the sensor rests on)
win_h      = 13;
win_y      = 0;     // move the window up (+) / down (-) to suit your plug position
flange_len = 22;    // mounting flange below the hood
screw_d    = 4.5;   // screw holes (#8 / M4 wood or machine screws)
csink_d    = 9;     // countersink diameter (0 = plain holes)

$fn = 48;

// ---------------------------------------------------------------- derived
iw = luna_w + 2 * clear;                 // inner width
ih = luna_h + 2 * clear;                 // inner height (sensor pocket)
ow = iw + 2 * wall;                      // outer width
y_bot = -ih / 2 - wall;                  // outer bottom
z_front = back + luna_d;                 // sensor front face
z_end = z_front + hood_len;              // visor tip
drop = tan(slope);
function top_in(z) = ih / 2 + top_gap - (z - z_front) * drop;   // visor underside at height z
end_gap = top_in(z_end) - ih / 2;
assert(end_gap >= 1, str("Visor tip is only ", end_gap,
       " mm above the sensor top - reduce slope or hood_len, or raise top_gap (keep it out of the beam)"));

module slab(y0, y1_back, y1_end, x0, x1, z0, z1) {
    // box from y0 up to a top that slopes linearly between z0 and z1
    hull() {
        translate([x0, y0, z0]) cube([x1 - x0, y1_back - y0, 0.01]);
        translate([x0, y0, z1 - 0.01]) cube([x1 - x0, y1_end - y0, 0.01]);
    }
}

module body() {
    // outer shell, sloped top, full length
    slab(y_bot, top_in(0) + wall, top_in(z_end) + wall, -ow / 2, ow / 2, 0, z_end);
    // mounting flange on the bed, below the hood
    translate([-ow / 2, y_bot - flange_len, 0]) cube([ow, flange_len + wall, back]);
}

module cuts() {
    // sensor pocket + headroom, open at the front
    slab(-ih / 2, top_in(back), top_in(z_end + 1), -iw / 2, iw / 2, back, z_end + 1);
    // floor removed in front of the sensor so water drains out
    translate([-iw / 2, y_bot - 1, z_front]) cube([iw, ih / 2 + 1 + wall, hood_len + 1]);
    // cable window
    translate([-win_w / 2, win_y - win_h / 2, -1]) cube([win_w, win_h, back + 2]);
    // drip groove across the underside of the visor near its tip
    if (groove > 0)
        translate([-iw / 2 - 0.01, top_in(z_end - 4) - groove * 0.6, z_end - 4 - groove])
            cube([iw + 0.02, groove * 0.6 + 0.8, groove]);   // 0.8 mm deep into the visor
    // screw holes (+ countersink on the front side, screw goes in from the hood side)
    for (sx = [-1, 1]) translate([sx * ow / 4, y_bot - flange_len / 2, -1]) {
        cylinder(d = screw_d, h = back + 2);
        if (csink_d > 0)
            translate([0, 0, back + 1 - (csink_d - screw_d) / 2])
                cylinder(d1 = screw_d, d2 = csink_d, h = (csink_d - screw_d) / 2 + 0.01);
    }
}

module ribs() {
    // two vertical ribs per side wall along the sensor depth
    if (rib > 0)
        for (sx = [-1, 1], sy = [-ih / 4, ih / 4])
            translate([sx * (iw / 2 - rib / 2), sy, back + luna_d / 2])
                cube([rib + 0.02, 2, luna_d - 3], center = true);
}

difference() { body(); cuts(); }
ribs();

echo(str("Outer size: ", ow, " x ", top_in(0) + wall - y_bot + flange_len, " x ", z_end, " mm (W x H x D)"));
echo(str("Visor tip clears the sensor top by ", end_gap, " mm"));
