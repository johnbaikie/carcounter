// 80 mm fan mount for a Raspberry Pi 5: a plate the fan screws onto, standing on
// four legs that screw into the Pi's mounting holes (the standoffs already there).
//
// Modelled in PRINT orientation: plate flat on the bed, legs pointing up. No supports.
// Installed it is flipped: legs down onto the Pi, fan on top of the plate.
//
// Assembly: screw the legs to the Pi first (M2.5 x 6 screws dropped down each hollow
// leg, a long thin screwdriver through the hole in the plate), then screw the fan on.
// Fan label facing DOWN (toward the Pi) = blows air onto the board.
//
// Check your fan with a ruler: 80 x 80 mm frame, holes 71.5 mm apart (standard).

/* [Fan] */
fan_size   = 80;      // square frame
fan_holes  = 71.5;    // hole spacing (centre to centre)
fan_screw  = 4.4;     // clearance for the fan's self-tapping screws (or M4 bolts)
opening_d  = 74;      // air opening under the blades
plate_t    = 3;       // plate thickness

/* [Raspberry Pi 5] */
pi_dx      = 58;      // mounting hole spacing along the board's long side
pi_dy      = 49;      //   ...and the short side
pi_screw   = 2.7;     // M2.5 clearance
clear_h    = 22;      // gap between the Pi's top surface and the plate
                      // (GPIO header + Dupont plugs are ~15 mm; leaves room for wire bends)
fan_off_x  = 0;       // shift the fan relative to the Pi's hole pattern (+x = toward USB ports)
fan_off_y  = 0;

/* [Legs] */
leg_d      = 8;       // leg outer diameter
leg_flare  = 12;      // wider where it meets the plate (stiffer)
leg_bore   = 5.2;     // hollow so an M2.5 screw head (4.5 mm) and a screwdriver fit inside
leg_floor  = 1.6;     // the screw head sits on this at the Pi end

$fn = 48;

// --------------------------------------------------------------- derived
legs = [for (sx = [-1, 1], sy = [-1, 1]) [sx * pi_dx / 2 - fan_off_x, sy * pi_dy / 2 - fan_off_y]];
leg_top = plate_t + clear_h;      // the Pi end of each leg

module rounded_square(s, r) {
    offset(r) square(s - 2 * r, center = true);
}

module plate() {
    linear_extrude(plate_t) rounded_square(fan_size, 4);
}

module leg_solid(p) {
    translate([p[0], p[1], 0]) {
        cylinder(d = leg_d, h = leg_top);
        cylinder(d1 = leg_flare, d2 = leg_d, h = plate_t + 6);   // fillet into the plate
    }
}

module leg_cut(p) {
    translate([p[0], p[1], 0]) {
        translate([0, 0, -1]) cylinder(d = leg_bore, h = leg_top - leg_floor + 1);  // screwdriver + head
        translate([0, 0, -1]) cylinder(d = pi_screw, h = leg_top + 2);             // screw shank
    }
}

difference() {
    union() {
        difference() {
            plate();
            translate([0, 0, -1]) cylinder(d = opening_d, h = plate_t + 2);
        }
        for (p = legs) leg_solid(p);
    }
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * fan_holes / 2, sy * fan_holes / 2, -1]) cylinder(d = fan_screw, h = plate_t + 2);
    for (p = legs) leg_cut(p);
}

echo(str("Plate ", fan_size, " x ", fan_size, " mm, overall height ", leg_top, " mm"));
