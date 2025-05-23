import numpy as np
import openmm
from openmm import app, unit
from openmm.unit import md_unit_system

from datetime import datetime

import time

import mdtraj as md

from tqdm import tqdm
import os

from calvados import build, interactions

from yaml import safe_load

from Bio.SeqUtils import seq3

from .components import *
from .traj_writer import TrajWriter

class Sim:
    def __init__(self,path,config,components):
        """
        simulate openMM Calvados;
        parameters are provided by config dictionary """

        self.path = path
        # parse config
        for key, val in config.items():
            setattr(self, key, val)

        for key, val in components['defaults'].items():
            setattr(self, f'default_{key}', val)

        self.comp_dict = components['system']
        self.comp_defaults = components['defaults']

        self.box = np.array(self.box)
        self.eps_lj *= 4.184 # kcal to kJ/mol

        if self.restart == 'checkpoint' and os.path.isfile(f'{self.path}/{self.frestart}'):
            self.slab_eq = False
            self.bilayer_eq = False

        if self.slab_eq:
            self.rcent = interactions.init_eq_restraints(self.box,self.k_eq)

    def make_components(self):
        self.components = np.empty(0)
        self.use_restraints = False
        # comp_setup = 'spiral' if self.topol=='shift_ref_bead' else 'linear'
        for name, properties in self.comp_dict.items():
            molecule_type = properties.get('molecule_type', self.default_molecule_type)
            if molecule_type == 'protein':
                # Protein component
                comp_setup = 'compact'
                comp = Protein(name, properties, self.comp_defaults)
            elif molecule_type in ['lipid','cooke_lipid']:
                # Lipid component
                comp_setup = 'linear'
                comp = Lipid(name, properties, self.comp_defaults)
            elif molecule_type in ['crowder']:
                # Crowder component
                comp_setup = 'compact'
                comp = Crowder(name, properties, self.comp_defaults)
            elif molecule_type in ['rna']:
                # Crowder component
                comp_setup = 'spiral'
                comp = RNA(name, properties, self.comp_defaults)
            else:
                # Generic component
                comp_setup = 'linear'
                comp = Component(name, properties, self.comp_defaults)

            comp.eps_lj = self.eps_lj
            comp.calc_properties(pH=self.pH, verbose=self.verbose, comp_setup=comp_setup)
            if comp.restraint:
                if comp.restraint_type == 'go':
                    comp.init_restraint_force(
                        eps_lj=self.eps_lj, cutoff_lj=self.cutoff_lj,
                        eps_yu=self.eps_yu, k_yu = self.k_yu
                    )
                else:
                    comp.init_restraint_force()
                self.use_restraints = True

            self.components = np.append(self.components, comp)

    def count_components(self):
        """ Count components and molecules. """

        self.ncomponents = 0
        self.nmolecules = 0

        for comp in self.components:
            self.ncomponents += 1
            self.nmolecules += comp.nmol

        print(f'Total number of components in the system: {self.ncomponents}')
        print(f'Total number of molecules in the system: {self.nmolecules}')

        # move lipids at the end of the array
        molecule_types = np.asarray([c.molecule_type for c in self.components])
        self.nlipids = np.sum([c.nmol if c.molecule_type == 'lipid' else 0 for c in self.components])
        self.ncookelipids = np.sum([c.nmol if c.molecule_type == 'cooke_lipid' else 0 for c in self.components])
        self.nproteins = np.sum([c.nmol if c.molecule_type == 'protein' else 0 for c in self.components])
        self.ncrowders = np.sum([c.nmol if c.molecule_type == 'crowder' else 0 for c in self.components])
        self.nrnas = np.sum([c.nmol if c.molecule_type == 'rna' else 0 for c in self.components])

        if ((self.ncomponents > 1) or (self.nmolecules > 1)) and (self.topol in ['single', 'center']):
            raise ValueError("Topol 'center' incompatible with multiple molecules.")

        # move proteins at the beginning of the array
        if self.nmolecules > self.nproteins:
            protein_components = self.components[np.where(molecule_types=='protein')]
            non_protein_components = self.components[np.where(molecule_types!='protein')]
            self.components = np.append(protein_components,non_protein_components)

    def build_system(self):
        """
        Set up system
        * component definitions
        * build particle coordinates
        * define interactions
        * set restraints
        """

        self.top = md.Topology()
        self.system = openmm.System()
        a, b, c = build.build_box(self.box[0],self.box[1],self.box[2])
        self.system.setDefaultPeriodicBoxVectors(a, b, c)


        # init interaction parameters (required before make components)
        self.eps_yu, self.k_yu = interactions.genParamsDH(self.temp,self.ionic)

        # make components
        self.make_components()
        self.count_components()

        # init interactions
        self.ah, self.yu = interactions.init_nonbonded_interactions(
            self.eps_lj,self.cutoff_lj,self.eps_yu,self.k_yu,self.cutoff_yu,self.fixed_lambda
            )
        if self.nlipids > 0:
            self.cos, self.cn = interactions.init_lipid_interactions(
            self.eps_lj,self.eps_yu,self.cutoff_yu,factor=1.9
            )
        if self.ncookelipids > 0:
            if self.nlipids > 0:
                raise
            self.cos, self.cn = interactions.init_lipid_interactions(
            self.eps_lj,self.eps_yu,self.cutoff_yu,factor=3.0
            )

        self.nparticles = 0 # bead counter
        self.grid_counter = 0 # molecule counter for xy and xyz grids

        self.pos = []

        if self.topol == 'slab': # proteins + rna
            self.xyzgrid = build.build_xyzgrid(self.nproteins+self.nrnas,[self.box[0],self.box[1],self.slab_width])
            self.xyzgrid += np.asarray([0,0,self.box[2]/2.-self.slab_width/2.])
            if self.ncrowders > 0: # crowder
                xyzgrid = build.build_xyzgrid(np.ceil(self.ncrowders/2.),[self.box[0],self.box[1],self.box[2]/2.-self.slab_outer])
                self.xyzgrid = np.append(self.xyzgrid, xyzgrid, axis=0)
                self.xyzgrid = np.append(self.xyzgrid, xyzgrid + np.asarray([0,0,self.box[2]/2.+self.slab_outer]), axis=0)
        elif self.topol == 'grid':
            self.xyzgrid = build.build_xyzgrid(self.nmolecules,self.box)
        if self.nlipids > 0:
            self.bilayergrid = build.build_xygrid(int(self.nlipids*1.05),self.box)
            if (self.nproteins + self.nrnas) > 0:
                xyzgrid = build.build_xyzgrid(np.ceil((self.nproteins+self.nrnas)/2.),[self.box[0],self.box[1],self.box[2]/2.-self.box[0]])
                self.xyzgrid = np.append(xyzgrid, xyzgrid + np.asarray([0,0,self.box[2]/2.+self.box[0]]), axis=0)
        if self.ncookelipids > 0:
            self.bilayergrid = build.build_xygrid(int(self.ncookelipids*1.05),self.box)
            if (self.nproteins + self.nrnas) > 0:
                xyzgrid = build.build_xyzgrid(np.ceil((self.nproteins+self.nrnas)/2.),[self.box[0],self.box[1],self.box[2]/2.-self.box[0]])
                self.xyzgrid = np.append(xyzgrid, xyzgrid + np.asarray([0,0,self.box[2]/2.+self.box[0]]), axis=0)

        for cidx, comp in enumerate(self.components):
            for idx in range(comp.nmol):
                if self.verbose:
                    print(f'Component {cidx}, Molecule {idx}: {comp.name}')
                # particle definitions
                self.add_mdtraj_topol(comp)
                self.add_particles_system(comp.mws)

                # add interactions + restraints
                if comp.molecule_type in ['protein','crowder']:
                    xs = self.place_molecule(comp)
                elif comp.molecule_type in ['lipid','cooke_lipid']:
                    xs = self.place_bilayer(comp)
                elif comp.molecule_type == 'rna':
                    xs = self.place_molecule(comp)
                self.add_interactions(comp)

                # add restraints towards box center
                if self.slab_eq and comp.molecule_type == 'protein':
                    self.add_eq_restraints(comp)

        self.pdb_cg = f'{self.path}/top.pdb'
        a = md.Trajectory(self.pos, self.top, 0, self.box, [90,90,90])
        if self.restart != 'pdb': # only save new topology if no system pdb is given
            a.save_pdb(self.pdb_cg)

        self.add_forces_to_system()
        self.print_system_summary()

    def add_forces_to_system(self):
        """ Add forces to system. """

        # Intermolecular forces
        for force in [self.yu, self.ah]:
            self.system.addForce(force)

        if (self.nlipids > 0) or (self.ncookelipids > 0):
            for force in [self.cos, self.cn]:
                self.system.addForce(force)

        # Intramolecular forces
        for comp in self.components:
            comp.get_forces() # bonded, angles, restraints...
            for force in comp.forces:
                self.system.addForce(force)
            if comp.restraint:
                print(f'Number of restraints for comp {comp.name}: {comp.cs.getNumBonds()}')

        # Equilibration forces
        if self.slab_eq:
            self.system.addForce(self.rcent)
        if self.box_eq:
            barostat = openmm.openmm.MonteCarloAnisotropicBarostat(
                    [self.pressure[0]*unit.bar,self.pressure[1]*unit.bar,self.pressure[2]*unit.bar],
                    self.temp*unit.kelvin,self.boxscaling_xyz[0],self.boxscaling_xyz[1],
                    self.boxscaling_xyz[2],1000)
            self.system.addForce(barostat)
        if self.bilayer_eq:
            barostat = openmm.openmm.MonteCarloMembraneBarostat(self.pressure[0]*unit.bar,
                    0*unit.bar*unit.nanometer, self.temp*unit.kelvin,
                    openmm.openmm.MonteCarloMembraneBarostat.XYIsotropic,
                    openmm.openmm.MonteCarloMembraneBarostat.ZFixed, 10000)
            self.system.addForce(barostat)

    def print_system_summary(self, write_xml: bool = True):
        """ Print system information and write xml. """

        if write_xml:
            with open(f'{self.path}/{self.sysname}.xml', 'w') as output:
                output.write(openmm.XmlSerializer.serialize(self.system))

        print(f'{self.nparticles} particles in the system')
        print('---------- FORCES ----------')
        print(f'ah: {self.ah.getNumParticles()} particles, {self.ah.getNumExclusions()} exclusions')
        print(f'yu: {self.yu.getNumParticles()} particles, {self.yu.getNumExclusions()} exclusions')
        if self.slab_eq:
            print(f'Equilibration restraints (rcent) towards box center in z direction')
            print(f'rcent: {self.rcent.getNumParticles()} restraints')
        if self.bilayer_eq:
            print(f'Equilibration under zero lateral tension')
        if self.box_eq:
            print(f'Equilibration through changes in box side lengths along '+' and '.join(np.array(['X','Y','Z'])[self.boxscaling_xyz]))

    def place_molecule(self, comp: Component, ntries: int = 10000):
        """
        Place proteins based on topology.
        """

        if self.topol == 'slab':
            x0 = self.xyzgrid[self.grid_counter]
            # x0[2] = self.box[2] / 2. # center in z
            xs = x0 + comp.xinit
            self.grid_counter += 1
        elif self.topol == 'grid':
            x0 = self.xyzgrid[self.grid_counter]
            xs = x0 + comp.xinit
            self.grid_counter += 1
        elif self.topol == 'center':
            x0 = self.box * 0.5 # place in center of box
            xs = x0 + comp.xinit
        elif self.topol == 'shift_ref_bead':
            x0 = self.box * 0.5 # place in center of box
            xs = x0 + comp.xinit
            xs -= comp.xinit[self.ref_bead]
        else:
            xs = build.random_placement(self.box, self.pos, comp.xinit, ntries=ntries)
        for x in xs:
            self.pos.append(x)
            self.nparticles += 1
        return xs # positions of the comp (to be used for restraints)

    def place_bilayer(self, comp: Component, ntries: int = 10000):
        """
        Place proteins based on topology.
        """
        #print('bilayergrid.shape',self.bilayergrid.shape)
        inserted = False
        while not inserted:
            xs, inserted = build.build_xybilayer(self.bilayergrid[0], self.box, self.pos, comp.xinit)
            if not inserted:
                xs, inserted = build.build_xybilayer(self.bilayergrid[0], self.box, self.pos, comp.xinit, upward=False)
                idx = np.random.randint(self.bilayergrid.shape[0])
                self.bilayergrid[0] = self.bilayergrid[idx]
                self.bilayergrid = np.delete(self.bilayergrid,idx,axis=0)
        for x in xs:
            self.pos.append(x)
            self.nparticles += 1
        return xs # positions of the comp (to be used for restraints)

    def add_bonds(self, comp, offset):
        """ Add bond forces. """

        exclusion_map = comp.add_bonds(offset)
        self.add_exclusions(exclusion_map)

    def add_angles(self, comp, offset):
        """ Add bond forces. """

        exclusion_map = comp.add_angles(offset)
        self.add_exclusions(exclusion_map)

    def add_restraints(self, comp, offset, min_scale = 0.1, exclude_nonbonded = True):
        """ Add restraints to single molecule. """
        # restr_pairlist = []

        exclusion_map = comp.add_restraints(offset, min_scale=min_scale)
        if exclude_nonbonded: # exclude ah, yu when restraining
            self.add_exclusions(exclusion_map)

    def add_exclusions(self, exclusion_map):
        # exclude LJ, YU for restrained pairs
        for excl in exclusion_map:
            self.ah = interactions.add_exclusion(self.ah, excl[0], excl[1])
            self.yu = interactions.add_exclusion(self.yu, excl[0], excl[1])
            if self.nlipids > 0 or self.ncookelipids > 0:
                self.cos.addExclusion(excl[0], excl[1])
                self.cn.addExclusion(excl[0], excl[1])

    def add_interactions(self,comp):
        """
        Protein interactions for one molecule of composition comp
        """

        offset = self.nparticles - comp.nbeads # to get indices of current comp in context of system

        # Add Ashbaugh-Hatch
        for sig, lam in zip(comp.sigmas, comp.lambdas):
            if comp.molecule_type in ['lipid', 'cooke_lipid']:
                self.ah.addParticle([sig*unit.nanometer, lam, 0])
            elif comp.molecule_type == 'crowder':
                self.ah.addParticle([sig*unit.nanometer, lam, -1])
            else: # protein, RNA
                self.ah.addParticle([sig*unit.nanometer, lam, 1])
            if self.nlipids > 0 or self.ncookelipids > 0:
                if comp.molecule_type in ['lipid', 'cooke_lipid']:
                    self.cos.addParticle([sig*unit.nanometer, lam, 0])
                else:
                    self.cos.addParticle([sig*unit.nanometer, lam, 1])
        # Add Debye-Huckel
        for q in comp.qs:
            self.yu.addParticle([q])

        # Add Charge-Nonpolar Interaction
        if self.nlipids > 0 or self.ncookelipids > 0:
            id_cn = 1 if comp.molecule_type == 'protein' else -1
            for sig, alpha, q in zip(comp.sigmas, comp.alphas, comp.qs):
                self.cn.addParticle([(sig/2)**3, alpha, q, id_cn])

        # Add bonds
        self.add_bonds(comp, offset)

        if comp.molecule_type == 'rna':
            self.add_angles(comp, offset)

        # Add restraints
        if comp.restraint:
            self.add_restraints(comp,offset)

        # write lists
        if self.verbose:
            comp.write_bonds(self.path)
            if comp.restraint:
                comp.write_restraints(self.path)

    def add_eq_restraints(self,comp):
        """ Add equilibration restraints. """

        offset = self.nparticles - comp.nbeads # to get indices of current comp in context of system
        for i in range(0,comp.nbeads):
            self.rcent.addParticle(i+offset)

    def add_mdtraj_topol(self, comp):
        """ Add one molecule to mdtraj topology. """

        # Note: Move this to component eventually.
        chain = self.top.add_chain()

        if comp.molecule_type == 'rna':
            for idx,resname in enumerate(comp.seq):
                res = self.top.add_residue(resname, chain, resSeq=idx+1)
                self.top.add_atom(resname+"P", element=md.element.phosphorus, residue=res)
                self.top.add_atom(resname+"N", element=md.element.nitrogen, residue=res)
            for i in range(comp.nbeads-1):
                for j in range(1,comp.nbeads):
                    if comp.bond_check(i,j):
                        self.top.add_bond(chain.atom(i), chain.atom(j))
        else:
            for idx,resname in enumerate(comp.seq):
                if comp.molecule_type == 'protein':
                    resname = str(seq3(resname)).upper()
                res = self.top.add_residue(resname, chain, resSeq=idx+1)
                self.top.add_atom('CA', element=md.element.carbon, residue=res)
            for i in range(chain.n_atoms-1):
                if comp.bond_check(i,i+1):
                    self.top.add_bond(chain.atom(i), chain.atom(i+1))


    def add_particles_system(self,mws):
        """ Add particles of one molecule to openMM system. """

        for mw in mws:
            self.system.addParticle(mw*unit.amu)

    def get_information(self, simulation, as_numpy=True, enforce_periodic_box=True):
        """Gets information (positions, forces and PE of system)
        Arguments:
            as_numpy: A boolean of whether to return as a numpy array
            enforce_periodic_box: A boolean of whether to enforce periodic boundary conditions
        Returns:
            positions: A numpy array of shape (n_atoms, 3) corresponding to the positions in nm
            velocities: A numpy array of shape (n_atoms, 3) corresponding to the velocities in nm/ps
            forces: A numpy array of shape (n_atoms, 3) corresponding to the force in kJ/mol*nm
            pe: A float coressponding to the potential energy in kJ/mol
            ke: A float coressponding to the kinetic energy in kJ/mol
            cell: A numpy array of shape (3, 3) corresponding to the cell vectors in nm
        """
        state = simulation.context.getState(
            getEnergy=True,
            getForces=True,
            getPositions=True,
            getVelocities=True,
            enforcePeriodicBox=enforce_periodic_box,
        )
        positions = state.getPositions(asNumpy=as_numpy).value_in_unit_system(
            md_unit_system
        )
        forces = state.getForces(asNumpy=as_numpy).value_in_unit_system(md_unit_system)
        velocities = state.getVelocities(asNumpy=as_numpy).value_in_unit_system(
            md_unit_system
        )
        pe = state.getPotentialEnergy().value_in_unit_system(md_unit_system)
        ke = state.getKineticEnergy().value_in_unit_system(md_unit_system)
        cell = state.getPeriodicBoxVectors(asNumpy=as_numpy).value_in_unit_system(
            md_unit_system
        )

        return positions, velocities, forces, pe, ke, cell

    def simulate(self):
        """ Simulate. """

        fcheck_in = f'{self.path}/{self.frestart}'
        fcheck_out = f'{self.path}/restart.chk'
        append = False

        if self.restart == 'pdb' and os.path.isfile(fcheck_in):
            pdb = app.pdbfile.PDBFile(fcheck_in)
        else:
            pdb = app.pdbfile.PDBFile(self.pdb_cg)

        # use langevin integrator
        integrator = openmm.openmm.LangevinMiddleIntegrator(self.temp*unit.kelvin,self.friction_coeff/unit.picosecond,0.01*unit.picosecond)
        if self.random_number_seed is not None:
            integrator.setRandomNumberSeed(self.random_number_seed)
        print(integrator.getFriction(),integrator.getTemperature())

        # assemble simulation
        platform = openmm.Platform.getPlatformByName(self.platform)
        if self.platform == 'CPU':
            simulation = app.simulation.Simulation(pdb.topology, self.system, integrator, platform, dict(Threads=str(self.threads)))
        else:
            if os.environ.get('CUDA_VISIBLE_DEVICES') is None:
                platform.setPropertyDefaultValue('DeviceIndex',str(self.gpu_id))
            simulation = app.simulation.Simulation(pdb.topology, self.system, integrator, platform)
        print('Running on', platform.getName())

        if (os.path.isfile(fcheck_in)) and (self.restart == 'checkpoint'):
            if not os.path.isfile(f'{self.path}/{self.sysname:s}.dcd'):
                raise Exception(f'Did not find {self.path}/{self.sysname:s}.dcd trajectory to append to!')
            append = True
            print(f'Reading check point file {fcheck_in}')
            print(f'Appending trajectory to {self.path}/{self.sysname:s}.dcd')
            print(f'Appending log file to {self.path}/{self.sysname:s}.log')
            simulation.loadCheckpoint(fcheck_in)
        else:
            if self.restart == 'pdb':
                print(f'Reading in system configuration {self.frestart}')
            elif self.restart == 'checkpoint':
                print(f'No checkpoint file {self.frestart} found: Starting from new system configuration')
            elif self.restart is None:
                print('Starting from new system configuration')
            else:
                raise

            if os.path.isfile(f'{self.path}/{self.sysname:s}.dcd'): # backup old dcd if not restarting from checkpoint
                now = datetime.now()
                dt_string = now.strftime("%Y%d%m_%Hh%Mm%Ss")
                print(f'Backing up existing {self.path}/{self.sysname:s}.dcd to {self.path}/backup_{self.sysname:s}_{dt_string}.dcd')
                os.system(f'mv {self.path}/{self.sysname:s}.dcd {self.path}/backup_{self.sysname:s}_{dt_string}.dcd')
            print(f'Writing trajectory to new file {self.path}/{self.sysname:s}.dcd')
            simulation.context.setPositions(pdb.positions)
            print(f'Minimizing energy.')
            simulation.minimizeEnergy()

        if self.slab_eq:
            print(f"Starting slab equilibration with k_eq == {self.k_eq:.4f} kJ/(mol*nm) for {self.steps_eq} steps", flush=True)
            simulation.reporters.append(app.dcdreporter.DCDReporter(f'{self.path}/equilibration_{self.sysname:s}.dcd',self.wfreq,append=append))
            h5_file_count = 0
            h5_file = TrajWriter(
                f'{self.path}/equilibration_{self.sysname:s}_{h5_file_count}.h5',
                self.nparticles,
                self.h5_freq,
                precision=64,
            )
            total_stages = int(self.steps_eq / self.save_freq)
            for _ in range(total_stages):
                simulation.step(self.save_freq)
                (
                    positions,
                    velocities,
                    forces,
                    pe,
                    ke,
                    cell,
                ) = self.get_information(simulation, as_numpy=True, enforce_periodic_box=True)
                if _ % self.h5_freq == 0 and _ != 0:
                    h5_file.close()
                    h5_file_count += 1
                    h5_file = TrajWriter(
                        f'{self.path}/equilibration_{self.sysname:s}_{h5_file_count}.h5',
                        self.nparticles,
                        self.h5_freq,
                        precision=64,
                    )
                h5_file.write_frame(positions, velocities, forces, pe, ke, cell)
            h5_file.early_close()
            state_final = simulation.context.getState(getPositions=True)
            rep = app.pdbreporter.PDBReporter(f'{self.path}/equilibration_final.pdb',0)
            rep.report(simulation,state_final)
            pdb = app.pdbfile.PDBFile(f'{self.path}/equilibration_final.pdb')

            for index, force in enumerate(self.system.getForces()):
                if isinstance(force, openmm.CustomExternalForce):
                    print(f'Removing external force {index}')
                    self.system.removeForce(index)
                    break
            integrator = openmm.openmm.LangevinIntegrator(self.temp*unit.kelvin,self.friction_coeff/unit.picosecond,0.01*unit.picosecond)
            if self.platform == 'CPU':
                simulation = app.simulation.Simulation(pdb.topology, self.system, integrator, platform, dict(Threads=str(self.threads)))
            else:
                simulation = app.simulation.Simulation(pdb.topology, self.system, integrator, platform)
            simulation.context.setPositions(pdb.positions)
            print(f'Minimizing energy.')
            simulation.minimizeEnergy()

        if self.box_eq or self.bilayer_eq:
            print(f"Starting pressure equilibration for {self.steps_eq} steps", flush=True)
            simulation.reporters.append(app.dcdreporter.DCDReporter(f'{self.path}/equilibration_{self.sysname:s}.dcd',self.wfreq,append=append))
            h5_file_count = 0
            h5_file = TrajWriter(
                f'{self.path}/equilibration_{self.sysname:s}_{h5_file_count}.h5',
                self.nparticles,
                self.h5_freq,
                precision=64,
            )
            total_stages = int(self.steps_eq / self.save_freq)
            for _ in range(total_stages):
                simulation.step(self.save_freq)
                (
                    positions,
                    velocities,
                    forces,
                    pe,
                    ke,
                    cell,
                ) = self.get_information(simulation, as_numpy=True, enforce_periodic_box=True)
                if _ % self.h5_freq == 0 and _ != 0:
                    h5_file.close()
                    h5_file_count += 1
                    h5_file = TrajWriter(
                        f'{self.path}/equilibration_{self.sysname:s}_{h5_file_count}.h5',
                        self.nparticles,
                        self.h5_freq,
                        precision=64,
                    )
                h5_file.write_frame(positions, velocities, forces, pe, ke, cell)
            h5_file.early_close()
            state_final = simulation.context.getState(getPositions=True,enforcePeriodicBox=True)
            rep = app.pdbreporter.PDBReporter(f'{self.path}/equilibration_final.pdb',0)
            rep.report(simulation,state_final)
            pdb = app.pdbfile.PDBFile(f'{self.path}/equilibration_final.pdb')
            topology = pdb.getTopology()
            a, b, c = state_final.getPeriodicBoxVectors()
            topology.setPeriodicBoxVectors(state_final.getPeriodicBoxVectors())
            for index, force in enumerate(self.system.getForces()):
                print(index,force)
            if not self.pressure_coupling:
                for index, force in enumerate(self.system.getForces()):
                    if isinstance(force, openmm.openmm.MonteCarloMembraneBarostat):
                        print(f'Removing barostat {index}')
                        self.system.removeForce(index)
                        break
                    if isinstance(force, openmm.openmm.MonteCarloAnisotropicBarostat):
                        print(f'Removing barostat {index}')
                        self.system.removeForce(index)
                        break
            for index, force in enumerate(self.system.getForces()):
                print(index,force)
            integrator = openmm.openmm.LangevinIntegrator(self.temp*unit.kelvin,self.friction_coeff/unit.picosecond,0.01*unit.picosecond)
            if self.platform == 'CPU':
                simulation = app.simulation.Simulation(topology, self.system, integrator, platform, dict(Threads=str(self.threads)))
            else:
                simulation = app.simulation.Simulation(topology, self.system, integrator, platform)
            simulation.context.setPositions(state_final.getPositions())
            simulation.context.setPeriodicBoxVectors(a, b, c)

        # run simulation
        simulation.reporters.append(app.dcdreporter.DCDReporter(f'{self.path}/{self.sysname:s}.dcd',self.wfreq,append=append))
        simulation.reporters.append(app.statedatareporter.StateDataReporter(f'{self.path}/{self.sysname}.log',self.logfreq,
                step=True,speed=True,elapsedTime=True,potentialEnergy=self.report_potential_energy,separator='\t',append=append))

        print("STARTING SIMULATION", flush=True)
        if self.runtime > 0: # in hours
            # convert to seconds
            runtime = self.runtime * 3600
            h5_file_count = 0
            h5_file = TrajWriter(
                f'{self.path}/{self.sysname:s}_{h5_file_count}.h5',
                self.nparticles,
                self.h5_freq,
                precision=64,
            )
            start_time = time.time()
            end_time = time.time()
            stage_count = 0
            while (end_time - start_time) < runtime:
                simulation.step(self.save_freq)
                (
                    positions,
                    velocities,
                    forces,
                    pe,
                    ke,
                    cell,
                ) = self.get_information(simulation, as_numpy=True, enforce_periodic_box=True)
                if stage_count % self.h5_freq == 0 and stage_count != 0:
                    h5_file.close()
                    h5_file_count += 1
                    h5_file = TrajWriter(
                        f'{self.path}/{self.sysname:s}_{h5_file_count}.h5',
                        self.nparticles,
                        self.h5_freq,
                        precision=64,
                    )
                h5_file.write_frame(positions, velocities, forces, pe, ke, cell)
                end_time = time.time()
                stage_count += 1
            h5_file.early_close()
        else:
            total_stages = int(self.steps / self.save_freq)
            h5_file_count = 0
            h5_file = TrajWriter(
                f'{self.path}/{self.sysname:s}_{h5_file_count}.h5',
                self.nparticles,
                self.h5_freq,
                precision=64,
            )
            for _ in range(total_stages):
                simulation.step(self.save_freq)
                (
                    positions,
                    velocities,
                    forces,
                    pe,
                    ke,
                    cell,
                ) = self.get_information(simulation, as_numpy=True, enforce_periodic_box=True)
                if _ % self.h5_freq == 0 and _ != 0:
                    h5_file.close()
                    h5_file_count += 1
                    h5_file = TrajWriter(
                        f'{self.path}/{self.sysname:s}_{h5_file_count}.h5',
                        self.nparticles,
                        self.h5_freq,
                        precision=64,
                    )
                h5_file.write_frame(positions, velocities, forces, pe, ke, cell)
            h5_file.early_close()
        simulation.saveCheckpoint(fcheck_out)

        now = datetime.now()
        dt_string = now.strftime("%Y%d%m_%Hh%Mm%Ss")

        state_final = simulation.context.getState(getPositions=True,enforcePeriodicBox=True)
        rep = app.pdbreporter.PDBReporter(f'{self.path}/{self.sysname}_{dt_string}.pdb',0)
        rep.report(simulation,state_final)
        rep = app.pdbreporter.PDBReporter(f'{self.path}/checkpoint.pdb',0)
        rep.report(simulation,state_final)

def run(path='.',fconfig='config.yaml',fcomponents='components.yaml'):
    with open(f'{path}/{fconfig}','r') as stream:
        config = safe_load(stream)

    with open(f'{path}/{fcomponents}','r') as stream:
        components = safe_load(stream)

    mysim = Sim(path,config,components)
    mysim.build_system()
    mysim.simulate()
    return mysim
